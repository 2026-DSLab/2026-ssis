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
    5. 최근 7일 시행분으로 WeeklyContract 를 조립해 out/ 에 JSON 저장

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


def main() -> int:
    settings = load_settings()
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

    print("\n" + "=" * 70)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
