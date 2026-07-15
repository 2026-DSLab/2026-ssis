"""개정 감지 → 확인된 변경을 구조화해 저장.

이 모듈은 워치리스트 항목 하나를 받아 "바뀌었는가"를 판정하고,
바뀌었다면 지금까지 만든 계층(api → parse → locate → db)을 순서대로
통과시켜 article_diff 에 결과를 남긴다.

이 파일이 하지 않는 것: 워치리스트 전체를 도는 주간 배치 루프,
스케줄링, 병렬 처리, 재시도 정책. 그건 이 함수를 호출하는 오케스트레이션
레이어(주간 배치 파이프라인)의 책임이며, 이 프로젝트에서 그 부분은
범위 밖이다. 여기서 제공하는 것은 "항목 하나를 정확하게 처리하는 방법"
까지다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from enum import Enum

from lawtrack.api.client import LawApiClient, LawApiError
from lawtrack.api.fulltext import fetch_admrul_fulltext, fetch_law_fulltext
from lawtrack.api.oldnew import fetch_admrul_oldnew, fetch_law_oldnew
from lawtrack.api.search import resolve_admrul, resolve_law
from lawtrack.db.repo import (
    ArticleDiffRepo,
    ChangeLogRepo,
    VersionRepo,
    WatchlistEntry,
    WatchlistRepo,
)
from lawtrack.locate.locator import locate_all
from lawtrack.parse.fulltext import flatten_searchable, parse_articles
from lawtrack.parse.oldnew import extract_changes

log = logging.getLogger(__name__)


class DetectStatus(str, Enum):
    UNCHANGED = "변경없음"
    CHANGED = "개정발생"
    NOT_FOUND = "조회결과없음"       # 워치리스트 등록 당시엔 있었는데 갑자기 0건 — 이상 신호
    AMBIGUOUS = "매칭모호"           # 완전일치가 2건 이상 — 사람 확인 필요
    NO_COMPARISON = "신구법없음"     # 개정은 됐으나 신구법 대비 불가 (제정 등)
    ERROR = "조회실패"


@dataclass(frozen=True)
class DetectResult:
    entry: WatchlistEntry
    status: DetectStatus
    current_serial_no: str = ""
    detail: str = ""
    search_result: object = None
    """LawSearchResult | AdmrulSearchResult | None.

    ★ 수정 사유: 최초 버전에서는 current_serial_no 만 넘겼는데, 이러면
    신구법 비교가 불가능한 경우(제정 등) process_law_entry 가 공포번호·
    시행일자·제개정구분명을 잃어버린다. oldAndNew 의 신조문_기본정보는
    이 필드들을 항상 포함한다는 보장이 없지만(실측: 사회보장기본법
    시행규칙 N-케이스에서는 공포번호/시행일자가 아예 없었음), 목록조회
    응답(LawSearchResult)은 이 필드들을 항상 포함한다(실측 확인).
    그래서 목록조회 결과 객체를 그대로 들고 다니게 한다.
    """


def detect_law(client: LawApiClient, version_repo: VersionRepo, entry: WatchlistEntry) -> DetectResult:
    """법령 워치리스트 1건의 개정 여부 판정.

    조회는 반드시 official_name(또는 law_id)으로 하고, 저장된 MST로
    조회하지 않는다 — MST로 조회하면 그 옛 버전만 돌아와 새 버전이
    생겼는지 알 수 없다.
    """
    dept_code = entry.dept_codes[0] if entry.dept_codes else None
    try:
        outcome = resolve_law(client, entry.official_name, dept_code=dept_code)
    except LawApiError as exc:
        log.error("법령 조회 실패 (law_id=%s): %s", entry.law_id, exc)
        return DetectResult(entry, DetectStatus.ERROR, detail=str(exc))

    if outcome.status == "not_found":
        log.warning(
            "현행 상태였던 법이 검색결과 0건: law_id=%s name=%s — "
            "제명변경/폐지 가능성. 수동 확인 필요.",
            entry.law_id, entry.official_name,
        )
        return DetectResult(entry, DetectStatus.NOT_FOUND)

    if outcome.status == "ambiguous":
        log.warning(
            "법령명 완전일치가 여러 건: law_id=%s name=%s candidates=%s",
            entry.law_id, entry.official_name, [c.law_id for c in outcome.candidates],
        )
        return DetectResult(entry, DetectStatus.AMBIGUOUS)

    current = outcome.candidates[0].serial_no
    if version_repo.law_exists(entry.law_id, current):
        return DetectResult(entry, DetectStatus.UNCHANGED, current, search_result=outcome.candidates[0])
    return DetectResult(entry, DetectStatus.CHANGED, current, search_result=outcome.candidates[0])


def detect_admrul(client: LawApiClient, version_repo: VersionRepo, entry: WatchlistEntry) -> DetectResult:
    """행정규칙 워치리스트 1건의 개정 여부 판정."""
    dept_name = None  # admrul 은 부처명 완전일치로 좁히므로 필요 시 entry 에 필드 추가
    try:
        outcome = resolve_admrul(client, entry.official_name, dept_name=dept_name)
    except LawApiError as exc:
        log.error("행정규칙 조회 실패 (law_id=%s): %s", entry.law_id, exc)
        return DetectResult(entry, DetectStatus.ERROR, detail=str(exc))

    if outcome.status == "not_found":
        log.warning(
            "현행 상태였던 행정규칙이 검색결과 0건: law_id=%s name=%s",
            entry.law_id, entry.official_name,
        )
        return DetectResult(entry, DetectStatus.NOT_FOUND)
    if outcome.status == "ambiguous":
        return DetectResult(entry, DetectStatus.AMBIGUOUS)

    current = outcome.candidates[0].serial_no
    if version_repo.admrul_exists(entry.law_id, current):
        return DetectResult(entry, DetectStatus.UNCHANGED, current, search_result=outcome.candidates[0])
    return DetectResult(entry, DetectStatus.CHANGED, current, search_result=outcome.candidates[0])


# ---------------------------------------------------------------------------
# 감지 → 조회 → 분석 → 저장 (항목 1건 전체 처리)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProcessOutcome:
    detect: DetectResult
    diff_count: int = 0
    located_success: int = 0
    located_failed: int = 0


def process_law_entry(
    client: LawApiClient,
    version_repo: VersionRepo,
    watchlist_repo: WatchlistRepo,
    change_log_repo: ChangeLogRepo,
    article_diff_repo: ArticleDiffRepo,
    entry: WatchlistEntry,
) -> ProcessOutcome:
    """법령 1건을 감지부터 저장까지 전부 처리.

    흐름: 감지 → (바뀌었으면) 본문조회 + 신구법조회 → <P> 추출 →
          위치확정(6가드) → article_diff 저장 → change_log 기록 →
          watchlist.last_serial_no 갱신.

    last_serial_no 갱신을 마지막에 두는 이유: 저장이 실패하면 갱신도
    안 되어야 다음 주에 재시도되기 때문이다. 갱신이 먼저 일어나고
    저장이 실패하면, 그 개정은 영원히 누락된다.
    """
    result = detect_law(client, version_repo, entry)
    if result.status is not DetectStatus.CHANGED:
        return ProcessOutcome(result)

    new_serial = result.current_serial_no
    sr = result.search_result  # LawSearchResult — 공포번호 등의 신뢰 가능한 출처

    oldnew = fetch_law_oldnew(client, new_serial)
    if not oldnew.available:
        log.info(
            "법령 %s(%s) 개정 감지되었으나 신구법 비교 불가 (%s) — 전문만 저장",
            entry.official_name, entry.law_id, oldnew.reason,
        )
        fulltext = fetch_law_fulltext(client, new_serial)
        version_repo.insert_law(entry.official_name, entry.law_id, new_serial, fulltext.raw)
        change_log_repo.insert(
            law_id=entry.law_id, new_serial_no=new_serial,
            old_serial_no=entry.last_serial_no,
            promulgation_no=sr.promulgation_no if sr else "",
            revision_type=sr.revision_type if sr else "",
            enforce_date=_parse_date(sr.enforce_date) if sr else None,
        )
        watchlist_repo.update_last_seen(entry.law_id, new_serial)
        return ProcessOutcome(DetectResult(entry, DetectStatus.NO_COMPARISON, new_serial))

    fulltext = fetch_law_fulltext(client, new_serial)
    version_repo.insert_law(entry.official_name, entry.law_id, new_serial, fulltext.raw)

    articles = parse_articles(fulltext.raw)
    units = flatten_searchable(articles)

    changes = extract_changes(oldnew.old_texts, oldnew.new_texts)
    located = locate_all(changes, units)

    # 시행일: 목록조회 결과(sr)를 우선하고, 없으면 oldAndNew 쪽을 보조로 쓴다.
    enforce_raw = (sr.enforce_date if sr else "") or oldnew.new_version.enforce_date
    enforce_date = _parse_date(enforce_raw) or date.today()
    saved = article_diff_repo.insert_results(
        entry.law_id, new_serial, located, default_enforce_date=enforce_date,
    )

    change_log_repo.insert(
        law_id=entry.law_id, new_serial_no=new_serial,
        old_serial_no=entry.last_serial_no or oldnew.old_version.serial_no,
        promulgation_no=(sr.promulgation_no if sr else "") or oldnew.new_version.promulgation_no,
        revision_type=(sr.revision_type if sr else "") or oldnew.new_version.revision_type,
        enforce_date=enforce_date,
    )
    watchlist_repo.update_last_seen(entry.law_id, new_serial)

    success = sum(1 for _c, rs in located for r in rs if r.status.value == "성공")
    failed = sum(
        1 for _c, rs in located for r in rs
        if r.status.value in ("0건실패", "중복실패")
    )
    log.info(
        "법령 %s(%s) 처리 완료: 저장 %d행, 위치확정 성공 %d/실패 %d",
        entry.official_name, entry.law_id, saved, success, failed,
    )
    return ProcessOutcome(result, diff_count=saved, located_success=success, located_failed=failed)


def _parse_date(yyyymmdd: str) -> date | None:
    yyyymmdd = (yyyymmdd or "").strip()
    if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    try:
        return date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))
    except ValueError:
        return None