"""주간 배치: 워치리스트 전체를 돌며 개정 감지 → 저장 → 산출물(JSON) 생성.

사용법 (law-tracking-db 폴더 루트, 가상환경 활성화 상태에서):

    python scripts\\run_weekly.py

이 스크립트가 하는 일 (순서대로):
    1. watchlist.due_for_activation() 으로 시행예정일이 도래한 항목을
       '현행'으로 전환 (감시 대상에 자동 편입)
    2. watchlist.active() 로 감시 대상 전체(status='현행') 조회
    3. 각 항목에 대해 detect.process_entry() 를 순차 실행
       (감지 → 본문/신구법 조회 → 위치확정 6가드 → article_diff/change_log 저장)
    4. 결과를 상태별로 집계해 요약 출력
    5. 이번 실행에서 새 버전으로 감지된 법령으로 WeeklyContract 를 조립하고,
       감지 버전·법령 ID·원문 URL·조문 구조를 코드로 교차 검증
    6. LLM API 키가 있으면 지정 모델로 구조화 요약을 생성한 뒤 독립 검증
       에이전트가 원문 근거와 대조. 실패 시 AI 요약을 보고서에서 제외
    7. 최종 JSON을 기반으로 읽기 쉬운 HWPX 주간보고서를 자동 생성

이 스크립트가 하지 않는 것 (detect.py 상단 docstring과 동일한 경계):
    병렬 처리, 재시도 정책, 실제 스케줄링(이 스크립트 자체를 매주
    자동으로 실행되게 cron/작업 스케줄러에 등록하는 것은 별도 인프라
    담당의 몫이다 — 여기서는 "한 번 실행하면 전체가 정확히 처리된다"는
    것만 보장한다).

한 항목에서 API 오류/예외가 나도 전체 배치를 중단하지 않고 나머지를
계속 처리한다 — 워치리스트 100건 중 1건이 실패했다고 나머지 99건의
개정 감지 기회를 날리면 안 되기 때문이다. 실패한 항목은 요약에 모아
보고하고, 종료 코드로 오류 발생 여부를 알린다(오류 0건이면 0, 있으면 1
— 향후 실제 스케줄러에 연결할 때 실패 알림 트리거로 쓸 수 있게).
"""

from __future__ import annotations

import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lawtrack.api.client import LawApiClient, LawApiError  # noqa: E402
from lawtrack.config import (  # noqa: E402
    ConfigError,
    configure_utf8_console,
    load_settings,
    setup_logging,
)
from lawtrack.contract.export import (  # noqa: E402
    build_contract_for_versions,
    write_contract,
)
from lawtrack.db.conn import Database  # noqa: E402
from lawtrack.db.repo import (  # noqa: E402
    ArticleDiffRepo,
    ChangeLogRepo,
    VersionRepo,
    WatchlistRepo,
)
from lawtrack.detect import DetectStatus, process_entry  # noqa: E402
from lawtrack.llm import (  # noqa: E402
    LLMSummaryError,
    SummaryVerificationError,
    summarize_contract,
    verification_disabled_report,
    verification_failure_report,
    verify_summary,
)
from lawtrack.report.hwpx import ReportBuildError, write_weekly_hwpx  # noqa: E402
from lawtrack.verify import (  # noqa: E402
    verify_source_integrity,
    write_verification_report,
)

log = logging.getLogger("run_weekly")

FAILURE_STATUSES = frozenset({
    DetectStatus.ERROR,
    DetectStatus.NOT_FOUND,
    DetectStatus.AMBIGUOUS,
})
REPORTABLE_STATUSES = frozenset({
    DetectStatus.CHANGED,
    DetectStatus.NO_COMPARISON,
})


def _provider_label(provider: str) -> str:
    return {
        "openai": "OpenAI",
        "openrouter": "OpenRouter",
        "openai-compatible": "OpenAI 호환 API",
    }.get(provider.lower(), provider)


def main() -> int:
    configure_utf8_console()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"❌ 설정 오류: {exc}")
        return 1
    setup_logging(settings.log_level)

    print("=" * 70)
    print(f"주간 배치 시작 — {date.today().isoformat()}")
    print("=" * 70)

    try:
        db = Database(settings.db)
    except ConnectionError as exc:
        print(f"\n❌ DB 연결 실패: {exc}")
        print("   .env 의 MYSQL_* 값을 확인하세요.")
        return 1

    if not db.ping():
        print("\n❌ DB ping 실패 — 서버가 떠 있는지, 접속 정보가 맞는지 확인하세요.")
        return 1
    print("\n✅ DB 연결 확인됨")

    watchlist_repo = WatchlistRepo(db)
    version_repo = VersionRepo(db)
    change_log_repo = ChangeLogRepo(db)
    article_diff_repo = ArticleDiffRepo(db)

    # --- 시행예정일 도래 항목 자동 편입 ---
    due = watchlist_repo.due_for_activation()
    if due:
        print(f"\n시행일 도래로 현행 전환: {len(due)}건")
        for entry in due:
            watchlist_repo.mark_status(entry.law_id, "현행")
            print(f"  → {entry.official_name} ({entry.law_id})")

    entries = watchlist_repo.active()
    print(f"\n감시 대상: {len(entries)}건")
    print("\n--- 개별 처리 시작 (실제 국가법령정보 API + 실제 DB) ---\n")

    counts: Counter[str] = Counter()
    errors: list[tuple[str, str, str]] = []
    detected_versions: set[tuple[str, str]] = set()
    no_comparison_versions: set[tuple[str, str]] = set()

    for idx, entry in enumerate(entries, 1):
        client = LawApiClient(settings.api)
        try:
            outcome = process_entry(
                client, version_repo, watchlist_repo, change_log_repo, article_diff_repo, entry,
            )
            status = outcome.detect.status
            counts[status.value] += 1
            current_serial = outcome.detect.current_serial_no
            if status in REPORTABLE_STATUSES and current_serial:
                version = (entry.law_id, current_serial)
                detected_versions.add(version)
                if status is DetectStatus.NO_COMPARISON:
                    no_comparison_versions.add(version)
            if status in REPORTABLE_STATUSES:
                marker = "🔶"
            elif status in FAILURE_STATUSES:
                marker = "❌"
                errors.append((
                    entry.law_id,
                    entry.official_name,
                    outcome.detect.detail or status.value,
                ))
            else:
                marker = "  "
            print(f"[{idx}/{len(entries)}]{marker} {entry.official_name} ({entry.law_id}): {status.value}")
            if status is DetectStatus.CHANGED:
                print(
                    f"      diff 저장 {outcome.diff_count}건 "
                    f"(위치확정 성공 {outcome.located_success}/실패 {outcome.located_failed})"
                )
        except LawApiError as exc:
            counts["API오류"] += 1
            errors.append((entry.law_id, entry.official_name, str(exc)))
            print(f"[{idx}/{len(entries)}] ❌ {entry.official_name} ({entry.law_id}): API 오류 — {exc}")
        except Exception as exc:  # noqa: BLE001 — 배치 전체를 죽이지 않기 위해 의도적으로 광범위하게 잡음
            counts["예외"] += 1
            errors.append((entry.law_id, entry.official_name, repr(exc)))
            log.exception("law_id=%s 처리 중 예외", entry.law_id)
            print(f"[{idx}/{len(entries)}] ❌ {entry.official_name} ({entry.law_id}): 예외 — {exc!r}")
        finally:
            client.close()

    print("\n" + "=" * 70)
    print("배치 결과 요약")
    print("=" * 70)
    for status, n in counts.most_common():
        print(f"  {status}: {n}건")

    if errors:
        print(f"\n⚠️ 오류 {len(errors)}건 — 해당 항목은 last_serial_no가 갱신되지 않았으므로 다음 배치에서 재시도됩니다:")
        for law_id, name, detail in errors:
            print(f"  {law_id} {name}: {detail}")

    # --- 산출물(JSON) 조립: 이번 실행에서 새 버전으로 감지된 법령 ---
    # 날짜 범위는 주간 보고서 표기용 메타데이터이며 시행일 필터가 아니다.
    to_date = date.today()
    from_date = to_date - timedelta(days=7)
    contract = build_contract_for_versions(
        watchlist_repo, article_diff_repo, change_log_repo,
        versions=detected_versions,
        no_comparison_versions=no_comparison_versions,
        from_date=from_date, to_date=to_date,
    )
    print(f"\n{contract.summary()}")

    source_report = verify_source_integrity(
        contract,
        expected_versions=detected_versions,
        processing_errors=errors,
        secrets=(settings.api.oc, settings.openai.api_key),
    )
    contract = contract.model_copy(update={"verification": source_report})
    verification_error = ""
    if source_report.status == "PASS":
        print(
            "✅ 법령 원본 무결성 검사 통과: "
            f"감지/계약 버전 {source_report.contract_version_count}건 일치"
        )
    else:
        verification_error = "법령 원본 무결성 검사 실패"
        print(
            "❌ 법령 원본 무결성 검사 실패 — AI 요약을 생성하지 않습니다: "
            f"문제 {len(source_report.issues)}건"
        )
        for issue in source_report.issues:
            print(
                f"   [{issue.code}] {issue.law_id} "
                f"{issue.new_serial_no} {issue.reason}".rstrip()
            )

    llm_error = ""
    if (
        source_report.status != "FAIL"
        and settings.openai.configured
        and contract.total_law_count
    ):
        provider_label = _provider_label(settings.openai.provider)
        print(f"\n{provider_label} 요약 생성 중: 모델 {settings.openai.model}")
        try:
            contract = summarize_contract(contract, settings.openai)
            print(
                f"✅ {provider_label} 요약 완료: 법령별 요약 "
                f"{len(contract.llm_summary.law_summaries) if contract.llm_summary else 0}건"
            )
            if settings.verification.enabled:
                print(
                    "독립 요약 검증 에이전트 실행 중: "
                    f"모델 {settings.verification.model}"
                )
                try:
                    report = verify_summary(
                        contract,
                        settings.openai,
                        settings.verification,
                        source_report,
                    )
                except SummaryVerificationError as exc:
                    log.warning("독립 요약 검증 실패: %s", exc)
                    report = verification_failure_report(
                        source_report,
                        contract,
                        settings.verification,
                        reason=str(exc),
                    )
                has_correctable_error = any(
                    issue.category == "SUMMARY" and issue.severity == "ERROR"
                    for issue in report.issues
                )
                if report.status == "FAIL" and has_correctable_error:
                    print("⚠️ 검증된 요약 오류를 반영해 AI 요약을 1회 자동 교정합니다.")
                    try:
                        contract = summarize_contract(
                            contract,
                            settings.openai,
                            verification_feedback=report,
                        )
                        report = verify_summary(
                            contract,
                            settings.openai,
                            settings.verification,
                            source_report,
                        )
                    except (LLMSummaryError, SummaryVerificationError) as exc:
                        log.warning("AI 요약 자동 교정 실패: %s", exc)
                        report = verification_failure_report(
                            source_report,
                            contract,
                            settings.verification,
                            reason=f"자동 교정 실패: {exc}",
                        )
                contract = contract.model_copy(update={"verification": report})
                print(
                    f"{'✅' if report.status == 'PASS' else '⚠️' if report.status == 'WARN' else '❌'} "
                    f"독립 요약 검증 결과: {report.status} "
                    f"(검증 법령 {report.checked_law_count}건, 문제 {len(report.issues)}건)"
                )
                for issue in report.issues:
                    if issue.category == "SOURCE":
                        continue
                    print(
                        f"   [{issue.severity}/{issue.code}] "
                        f"{issue.law_id} {issue.location} {issue.reason}".rstrip()
                    )
                if report.status == "FAIL":
                    if settings.verification.fail_closed:
                        contract = contract.model_copy(update={
                            "llm_summary": None,
                            "verification": report,
                        })
                        print(
                            "   검증 실패로 AI 요약을 제외하고 규칙 기반 보고서로 전환합니다."
                        )
                    if settings.verification.required:
                        verification_error = "독립 LLM 요약 검증 실패"
            else:
                report = verification_disabled_report(
                    source_report,
                    contract,
                    settings.verification,
                )
                contract = contract.model_copy(update={"verification": report})
                print("⚠️ AI 요약은 생성됐지만 독립 검증 에이전트가 비활성화되어 있습니다.")
        except LLMSummaryError as exc:
            log.warning("%s 요약 실패: %s", provider_label, exc)
            print(f"⚠️ {provider_label} 요약 실패 — 규칙 기반 보고서로 계속합니다: {exc}")
            if settings.openai.required:
                llm_error = str(exc)
    elif source_report.status == "FAIL":
        pass
    elif settings.openai.configured:
        print("\nLLM 요약 대상 개정 법령이 없어 API를 호출하지 않습니다.")
    else:
        print("\nLLM 요약 건너뜀: .env에 OPENAI_API_KEY를 설정하면 자동 활성화됩니다.")

    output_path = write_contract(contract, settings.export.output_dir)
    print(f"산출물 저장됨: {output_path}")
    if contract.verification is not None:
        verification_path = write_verification_report(
            contract.verification,
            settings.export.output_dir,
            batch_date=contract.batch_date,
        )
        print(f"검증 보고서 저장됨: {verification_path}")

    report_error = ""
    report_path = settings.export.output_dir / f"weekly_law_report_{contract.batch_date}.hwpx"
    try:
        report_result = write_weekly_hwpx(contract, report_path)
        actual_report_path = Path(report_result["path"])
        if actual_report_path != report_path:
            print(f"⚠️ 기존 HWPX가 열려 있어 새 파일명으로 저장했습니다: {actual_report_path}")
        else:
            print(f"HWPX 보고서 저장됨: {actual_report_path}")
    except ReportBuildError as exc:
        report_error = str(exc)
        log.exception("HWPX 보고서 생성 실패")
        print(f"❌ HWPX 보고서 생성 실패: {exc}")

    print("\n" + "=" * 70)
    return 1 if errors or report_error or llm_error or verification_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
