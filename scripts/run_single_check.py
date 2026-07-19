"""단일 법령 전체 파이프라인 실행 테스트.

실제 국가법령정보 API + 실제 DB를 사용해, 감지→조회→분석→저장 전 과정을
한 건에 대해 실행하고 결과를 상세히 출력한다.

사용법 (law-tracking-db 폴더 루트, 가상환경 활성화 상태에서):

    python scripts\\run_single_check.py                # 기본값: 전자정부법(009199)
    python scripts\\run_single_check.py 001973          # 국민기초생활보장법으로 테스트
    python scripts\\run_single_check.py 33483            # 정보시스템 감리기준(행정규칙)

law_id 는 watchlist 테이블에 등록된 값이어야 한다.
"""

from __future__ import annotations

import logging
import sys

from lawtrack.config import load_settings, setup_logging
from lawtrack.api.client import LawApiClient, LawApiError
from lawtrack.db.conn import Database
from lawtrack.db.repo import (
    ArticleDiffRepo,
    ChangeLogRepo,
    VersionRepo,
    WatchlistRepo,
)
from lawtrack.detect import DetectStatus, process_entry

log = logging.getLogger("run_single_check")


def _extract_word_changes(old_text: str, new_text: str) -> list[tuple[str, str]]:
    """단어 단위로 두 문장을 대조해 (바뀌기 전, 바뀐 후) 쌍의 목록을 만든다.

    difflib.SequenceMatcher 로 안 바뀐 구간(equal)을 건너뛰고, 바뀐 구간
    (replace/delete/insert)만 뽑아 앞뒤 단어를 그대로 묶는다. 국방데이터·
    인공지능업무 훈령 실측 사례처럼 한 문장 안에 바뀐 곳이 여러 군데면
    쌍이 여러 개 나온다.
    """
    import difflib

    old_words = old_text.split()
    new_words = new_text.split()
    if not old_words and not new_words:
        return []

    matcher = difflib.SequenceMatcher(None, old_words, new_words)
    pairs: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before = " ".join(old_words[i1:i2])
        after = " ".join(new_words[j1:j2])
        pairs.append((before, after))
    return pairs


def main() -> int:
    settings = load_settings()
    setup_logging(settings.log_level)

    law_id = sys.argv[1] if len(sys.argv) > 1 else "009199"  # 기본: 전자정부법

    print("=" * 70)
    print(f"단일 법령 파이프라인 테스트 — law_id={law_id}")
    print("=" * 70)

    # --- DB 연결 확인 ---
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

    entry = watchlist_repo.get(law_id)
    if entry is None:
        print(f"\n❌ watchlist 에 law_id={law_id} 가 없습니다. seed_watchlist.sql 을 확인하세요.")
        return 1

    print(f"\n대상: {entry.official_name} ({entry.law_id}, {entry.law_type})")
    print(f"등록된 마지막 확인 버전(last_serial_no): {entry.last_serial_no}")

    # --- API 클라이언트 ---
    client = LawApiClient(settings.api)

    print("\n--- API 호출 시작 (실제 국가법령정보 서버) ---")
    try:
        outcome = process_entry(
            client, version_repo, watchlist_repo, change_log_repo, article_diff_repo, entry,
        )
    except LawApiError as exc:
        print(f"\n❌ API 호출 실패: {exc}")
        print("   OC 키, 서버 IP 등록 상태를 확인하세요.")
        return 1
    except Exception:
        log.exception("예상치 못한 오류")
        return 1
    finally:
        client.close()

    # --- 결과 출력 ---
    print("\n" + "=" * 70)
    print("결과")
    print("=" * 70)
    d = outcome.detect
    print(f"판정 상태        : {d.status.value}")
    print(f"현재 버전(API)   : {d.current_serial_no or '-'}")

    if d.status is DetectStatus.UNCHANGED:
        print("\n→ 지난 확인 이후 개정된 내용이 없습니다. (정상 — 재실행해도 계속 이 상태일 수 있음)")

    elif d.status is DetectStatus.CHANGED:
        print(f"저장된 조문 diff 행 수 : {outcome.diff_count}")
        print(f"위치확정 성공          : {outcome.located_success}")
        print(f"위치확정 실패          : {outcome.located_failed}")

        print("\n--- DB에 실제로 저장된 조문 diff 상세 ---")
        rows = [
            r for r in article_diff_repo.fetch_period(
                __import__("datetime").date(2000, 1, 1),
                __import__("datetime").date(2100, 1, 1),
            )
            if r["law_id"] == law_id and r["law_serial_no"] == d.current_serial_no
        ]
        if not rows:
            print("  (저장된 행 없음 — 전부 실패했을 수 있음, 위 located_failed 확인)")
        for r in rows:
            loc = f"{r.get('article_label','')}{r.get('clause_no','')}{r.get('item_label','')}{r.get('subitem_label','')}"
            print(f"\n  [{loc}] {r['change_type']} / {r['match_status']}")

            # 1) 바뀐 부분만 — "A → B" 형태로 깔끔하게 (한 문장에 여러 곳 바뀌어도 각각 표시)
            changes = _extract_word_changes(r.get("old_text") or "", r.get("new_text") or "")
            if changes:
                print("    ▸ 바뀐 부분만:")
                for before, after in changes:
                    before_disp = before if before else "(없음, 신설)"
                    after_disp = after if after else "(삭제됨)"
                    print(f"        {before_disp}  →  {after_disp}")
            else:
                print("    ▸ 바뀐 부분만: (텍스트 차이 없음)")

            # 2) 전체 문장 — 조문 전문 그대로
            if r.get("old_text"):
                print(f"    전체(전): {r['old_text']}")
            if r.get("new_text"):
                print(f"    전체(후): {r['new_text']}")

        failures = article_diff_repo.fetch_failures(limit=20)
        my_failures = [f for f in failures if f["law_id"] == law_id]
        if my_failures:
            print(f"\n--- 위치확정 실패 상세 ({len(my_failures)}건) ---")
            for f in my_failures:
                print(f"  {f['match_status']}: {f.get('match_detail')}")

    elif d.status is DetectStatus.NO_COMPARISON:
        print("\n→ 개정은 감지됐으나 신구법 비교가 불가능합니다 (제정 직후 등). 전문만 저장됨.")

    elif d.status is DetectStatus.NOT_FOUND:
        print("\n→ ⚠️ 검색결과 0건입니다. 법명이 바뀌었거나 폐지됐을 수 있습니다.")
        print(f"   등록된 official_name: {entry.official_name}")
        print("   law.go.kr 에서 이 법을 직접 검색해 정식명칭을 확인하세요.")

    elif d.status is DetectStatus.AMBIGUOUS:
        print("\n→ ⚠️ 완전일치가 여러 건입니다. 소관부처 조건을 추가해야 합니다.")

    elif d.status is DetectStatus.ERROR:
        print(f"\n→ ❌ 조회 오류: {d.detail}")

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())