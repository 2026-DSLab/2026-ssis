"""주간 배치: 개정 감지 → 조문 비교 → (선택) LLM 요약 → (선택) HWPX 보고서.

사용법 (프로젝트 루트, 가상환경 활성화 상태에서):

    python scripts\\run_weekly.py              # 감지·비교만 (LLM 비용 없음)
    python scripts\\run_weekly.py --summarize  # + LLM 요약 JSON
    python scripts\\run_weekly.py --full       # + HWPX 보고서 + DB 적재

이 스크립트가 하는 일 (순서대로):
    1. watchlist.due_for_activation() 으로 시행예정일이 도래한 항목을
       '현행'으로 전환 (감시 대상에 자동 편입)
    2. watchlist.active() 로 감시 대상 전체(status='현행') 조회
    3. 각 항목에 대해 detect.process_entry() 를 순차 실행
       (감지 → 본문/신구법 조회 → 위치확정 6가드 → article_diff/change_log 저장)
    4. 결과를 상태별로 집계해 요약 출력
    5. 최근 7일 시행분으로 WeeklyContract 를 조립해 out/ 에 JSON 저장
    6. (--summarize) 그 JSON 을 요약 파이프라인에 넘겨 요약 생성
    7. (--hwpx / --summary-db) 보고서 생성 / law_summary 테이블 적재

★ 왜 요약이 기본값이 아닌가:
    1~5 단계는 공짜지만 6단계부터는 호출 건당 LLM 비용이 든다. 기본을
    켜 두면 "감지 결과만 보려고" 돌린 실행에서도 조용히 과금된다.
    자동 실행(작업 스케줄러)에는 --full 로 등록한다 — scripts/weekly.cmd
    가 그렇게 되어 있다.

이 스크립트가 하지 않는 것 (detect.py 상단 docstring과 동일한 경계):
    병렬 처리, 재시도 정책. 스케줄 등록 자체는 scripts/register_task.ps1
    이 맡는다 — 여기서는 "한 번 실행하면 전체가 정확히 처리된다"는 것만
    보장한다.

한 항목에서 API 오류/예외가 나도 전체 배치를 중단하지 않고 나머지를
계속 처리한다 — 워치리스트 100건 중 1건이 실패했다고 나머지 99건의
개정 감지 기회를 날리면 안 되기 때문이다. 실패한 항목은 요약에 모아
보고하고, 종료 코드로 오류 발생 여부를 알린다(오류 0건이면 0, 있으면 1
— 스케줄러의 실패 알림 트리거로 쓸 수 있게).
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from lawtrack.api.client import LawApiClient, LawApiError
from lawtrack.config import load_settings, setup_logging
from lawtrack.contract.export import build_contract, write_contract
from lawtrack.db.conn import Database
from lawtrack.db.repo import (
    ArticleDiffRepo,
    ChangeLogRepo,
    VersionRepo,
    WatchlistRepo,
)
from lawtrack.detect import DetectStatus, process_entry

log = logging.getLogger("run_weekly")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_weekly",
        description="법령/행정규칙 개정 주간 배치 (감지 → 비교 → 요약 → 보고서)",
    )
    parser.add_argument(
        "--summarize", action="store_true",
        help="감지 후 LLM 요약까지 실행 (호출 건당 API 비용 발생)",
    )
    parser.add_argument(
        "--hwpx", action="store_true",
        help="HWPX 보고서까지 생성 (--summarize 를 포함한다)",
    )
    parser.add_argument(
        "--summary-db", action="store_true",
        help="요약을 law_summary 테이블에 적재 (--summarize 를 포함한다)",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="--summarize --hwpx --summary-db 를 모두 켠다. 자동 실행용.",
    )
    args = parser.parse_args(argv)

    # --hwpx / --summary-db 는 요약 결과가 있어야 의미가 있다. 사용자가
    # --summarize 를 빠뜨렸다고 "아무것도 안 나오는" 실행을 하게 두지 않는다.
    if args.full:
        args.summarize = args.hwpx = args.summary_db = True
    if args.hwpx or args.summary_db:
        args.summarize = True
    return args


def run_summary_stage(
    contract_path: Path, db: Database, *, hwpx: bool, to_db: bool,
) -> int:
    """계약 JSON 하나를 요약 파이프라인에 넘긴다. 오류 건수를 돌려준다.

    ★ import 를 함수 안에서 하는 이유: 요약 단계는 LLM SDK 와 hwpx 를
      필요로 하는데, 감지만 쓰는 사람은 그것들을 깔지 않았을 수 있다.
      모듈 최상단에서 import 하면 --summarize 를 안 쓴 실행도 함께
      죽는다.

    ★ 여기서 예외를 잡아 삼키지 않고 건수로 돌려주는 이유: 감지 결과는
      이미 DB 에 커밋되어 안전하다. 요약이 실패했다고 그 사실을 되돌릴
      수는 없으므로, 배치는 계속 진행하되 종료 코드로 알린다.
    """
    print("\n" + "=" * 70)
    print("요약 단계 시작 (LLM)")
    print("=" * 70)

    try:
        from summarizer.config import ConfigError as SummaryConfigError
        from summarizer.config import load_settings as load_summary_settings
        from summarizer.llm import build_client
        from summarizer.pipeline import SummaryPipeline
        from summarizer.sinks import DbSink, HwpxSink, JsonSink
    except ImportError as exc:
        print(f"\n❌ 요약 모듈을 불러올 수 없습니다: {exc}")
        print("   pip install -e \".[openai]\" 로 의존성을 설치하세요.")
        return 1

    try:
        summary_settings = load_summary_settings()
    except SummaryConfigError as exc:
        print(f"\n❌ 요약 설정 오류: {exc}")
        return 1

    try:
        client = build_client(summary_settings.llm)
        results = SummaryPipeline(client, summary_settings).run([contract_path])
    except Exception as exc:  # noqa: BLE001 — 감지 결과를 지키기 위해 여기서 멈춘다
        log.exception("요약 파이프라인 실패")
        print(f"\n❌ 요약 실패: {exc!r}")
        print("   감지·비교 결과는 이미 DB 와 out/ 에 저장되어 있습니다.")
        return 1

    JsonSink(summary_settings.pipeline.output_dir).write(results)
    print(f"요약 저장됨: {summary_settings.pipeline.output_dir}")

    errors = 0
    for contract in results:
        for law in contract.laws:
            print(f"  ■ [{law.law_type}] {law.law_name}: {law.headline}")
            for caveat in law.caveats:
                print(f"    ※ {caveat}")

    if hwpx:
        try:
            report_dir = summary_settings.pipeline.output_dir.parent / "reports"
            HwpxSink(report_dir).write(results)
            print(f"보고서 저장됨: {report_dir}")
        except Exception as exc:  # noqa: BLE001
            log.exception("HWPX 보고서 생성 실패")
            print(f"\n⚠️ 보고서 생성 실패: {exc!r} (요약 JSON 은 저장됨)")
            errors += 1

    if to_db:
        try:
            saved = DbSink(
                db,
                llm_provider=summary_settings.llm.provider,
                llm_model=summary_settings.llm.model,
            ).write(results)
            print(f"law_summary 적재: {saved}건")
        except Exception as exc:  # noqa: BLE001
            log.exception("요약 DB 적재 실패")
            print(f"\n⚠️ 요약 DB 적재 실패: {exc!r} (요약 JSON 은 저장됨)")
            errors += 1

    return errors


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_level)

    print("=" * 70)
    print(f"주간 배치 시작 — {date.today().isoformat()}")
    print("=" * 70)

    try:
        db = Database(settings.db)
    except ConnectionError as exc:
        print(f"\n❌ DB 연결 실패: {exc}")
        print("   .env 의 POSTGRES_* 값을 확인하세요.")
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

    for idx, entry in enumerate(entries, 1):
        client = LawApiClient(settings.api)
        try:
            outcome = process_entry(
                client, version_repo, watchlist_repo, change_log_repo, article_diff_repo, entry,
            )
            status = outcome.detect.status
            counts[status.value] += 1
            marker = "🔶" if status is DetectStatus.CHANGED else "  "
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

    # --- 산출물(JSON) 조립: 최근 7일 시행분 ---
    to_date = date.today()
    from_date = to_date - timedelta(days=7)
    contract = build_contract(
        watchlist_repo, article_diff_repo, change_log_repo,
        from_date=from_date, to_date=to_date,
    )
    print(f"\n{contract.summary()}")

    output_path = write_contract(contract, Path("out"))
    print(f"산출물 저장됨: {output_path}")

    # --- 요약 → 보고서 → DB 적재 ---
    summary_errors = 0
    if args.summarize:
        summary_errors = run_summary_stage(
            output_path, db, hwpx=args.hwpx, to_db=args.summary_db,
        )

    print("\n" + "=" * 70)
    return 1 if (errors or summary_errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
