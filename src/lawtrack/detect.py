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
from dataclasses import asdict, dataclass
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
from lawtrack.parse.fulltext import flatten_searchable, parse_admrul_units, parse_articles
from lawtrack.parse.oldnew import extract_admrul_unchanged, extract_changes
from lawtrack.text.split import strip_annotations

log = logging.getLogger(__name__)

#: watchlist.law_type 값 중 행정규칙을 가리키는 값. process_entry() 의 분기 기준.
ADMRUL_LAW_TYPE = "행정규칙"


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
    """행정규칙 워치리스트 1건의 개정 여부 판정.

    ★ 실측 발견(2026-07-16): dept_name 이 entry.dept_codes 와 무관하게
    항상 None으로 하드코딩돼 있었다 — watchlist.dept_codes 를 채워도
    실제로는 전혀 쓰이지 않아, 동명이인 행정규칙(예: "협상에 의한
    계약체결기준" — 재정경제부판/타부처판)이 완전일치 2건 이상으로
    걸리면 dept_codes 를 아무리 채워도 영구히 AMBIGUOUS 로 막혔다.
    entry.dept_codes 는 법령용으로는 부처 "코드"를 담지만, admrul
    쪽 API는 부처 "이름" 완전일치로만 좁혀지므로(api/search.py
    resolve_admrul 참고) 같은 필드에 부처 이름 문자열을 담아 재사용한다.
    """
    dept_name = entry.dept_codes[0] if entry.dept_codes else None
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
        version_repo.insert_law(
            entry.official_name, entry.law_id, new_serial, fulltext.raw,
            parsed_articles=_serialize_articles(parse_articles(fulltext.raw)),
        )
        # ★ 실측 발견(2026-07-18): enforce_date를 None으로 그냥 두면(sr이 없는
        # 경우) contract/export.py의 fetch_no_comparison_in_period가 이 행을
        # 기간(BETWEEN) 조회로 영원히 못 찾는다 — 다른 브랜치처럼 오늘 날짜로
        # 보강한다.
        change_log_repo.insert(
            law_id=entry.law_id, new_serial_no=new_serial,
            old_serial_no=entry.last_serial_no,
            promulgation_no=sr.promulgation_no if sr else "",
            revision_type=sr.revision_type if sr else "",
            revision_reason=fulltext.revision_reason,
            enforce_date=(_parse_date(sr.enforce_date) if sr else None) or date.today(),
            comparison_available=False,
        )
        watchlist_repo.update_last_seen(entry.law_id, new_serial)
        return ProcessOutcome(DetectResult(entry, DetectStatus.NO_COMPARISON, new_serial))

    fulltext = fetch_law_fulltext(client, new_serial)
    articles = parse_articles(fulltext.raw)
    version_repo.insert_law(
        entry.official_name, entry.law_id, new_serial, fulltext.raw,
        parsed_articles=_serialize_articles(articles),
    )

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
        revision_reason=fulltext.revision_reason,
        enforce_date=enforce_date,
        unchanged_clauses=_unchanged_clauses(articles, located),
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


def process_admrul_entry(
    client: LawApiClient,
    version_repo: VersionRepo,
    watchlist_repo: WatchlistRepo,
    change_log_repo: ChangeLogRepo,
    article_diff_repo: ArticleDiffRepo,
    entry: WatchlistEntry,
) -> ProcessOutcome:
    """행정규칙 1건을 감지부터 저장까지 전부 처리 — process_law_entry 의 admrul판.

    ★ 실측 발견(2026-07-16): scripts/run_single_check.py 등 실행 스크립트가
    entry.law_type 과 무관하게 항상 process_law_entry (target=law API) 만
    호출하고 있었다. 행정규칙(25건 중 하나인 정보시스템 감리기준, law_id=33483)
    으로 실행하면 target=law 검색은 애초에 대상이 아니므로 항상 0건이 나와
    "조회결과없음(제명변경/폐지 가능성)"으로 오판되었다 — 실제로는 목록조회
    API를 잘못 골랐을 뿐, 그 행정규칙은 정상적으로 존재한다. 이 함수가 그
    누락됐던 admrul 쓰기 경로다. process_entry() 로 law_type 에 따라 자동
    분기하는 것을 권장한다.

    법령과의 구조적 차이 (api/oldnew.py, api/fulltext.py 실측 확인):
        - 목록조회: dept_code(코드) 대신 dept_name(이름) 으로 완전일치 좁힘
        - 본문조회: MST= 대신 ID=(행정규칙일련번호) 파라미터
        - 신구법"없음" 판정: 필드값(신구법존재여부=N) 이 아니라 비-JSON
          응답 자체("<Law>일치하는 신구법 없습니다.</Law>")로 판정됨
          (oldnew.available=False 로 이미 흡수되어 있어 이 함수 입장에서는
          법령과 동일하게 처리하면 된다)
    """
    result = detect_admrul(client, version_repo, entry)
    if result.status is not DetectStatus.CHANGED:
        return ProcessOutcome(result)

    new_serial = result.current_serial_no
    sr = result.search_result  # AdmrulSearchResult

    oldnew = fetch_admrul_oldnew(client, new_serial)
    if not oldnew.available:
        log.info(
            "행정규칙 %s(%s) 개정 감지되었으나 신구법 비교 불가 (%s) — 전문만 저장",
            entry.official_name, entry.law_id, oldnew.reason,
        )
        fulltext = fetch_admrul_fulltext(client, new_serial)
        version_repo.insert_admrul(
            entry.official_name, entry.law_id, new_serial, fulltext.raw,
            parsed_units=_serialize_units(parse_admrul_units(fulltext.raw)),
        )
        change_log_repo.insert(
            law_id=entry.law_id, new_serial_no=new_serial,
            old_serial_no=entry.last_serial_no,
            promulgation_no=sr.promulgation_no if sr else "",
            revision_type=sr.revision_type if sr else "",
            revision_reason=fulltext.revision_reason,
            enforce_date=(_parse_date(sr.promulgation_date) if sr else None) or date.today(),
            comparison_available=False,
        )
        watchlist_repo.update_last_seen(entry.law_id, new_serial)
        return ProcessOutcome(DetectResult(entry, DetectStatus.NO_COMPARISON, new_serial))

    fulltext = fetch_admrul_fulltext(client, new_serial)
    # ★ 실측(2026-07-16): 행정규칙은 법령과 본문 구조가 전혀 달라(조문/항/호가
    # JSON 트리로 안 쪼개져 있고 평문 한 줄에 통째로 이어붙어 있음)
    # parse_articles+flatten_searchable(법령 전용)를 그대로 쓰면 조문을 0건
    # 찾아 모든 위치확정이 100% 실패한다. 전용 파서를 쓴다.
    units = parse_admrul_units(fulltext.raw)
    version_repo.insert_admrul(
        entry.official_name, entry.law_id, new_serial, fulltext.raw,
        parsed_units=_serialize_units(units),
    )

    changes = extract_changes(oldnew.old_texts, oldnew.new_texts)
    located = locate_all(changes, units)

    # 시행일: 행정규칙 목록조회에는 시행일자가 없으므로(발령일자만 있음),
    # 목록조회(sr)가 있으면 발령일을 시행일 대용으로, 없으면 oldAndNew 쪽을 보조로 쓴다.
    enforce_raw = (sr.promulgation_date if sr else "") or oldnew.new_version.enforce_date
    enforce_date = _parse_date(enforce_raw) or date.today()
    saved = article_diff_repo.insert_results(
        entry.law_id, new_serial, located, default_enforce_date=enforce_date,
    )

    change_log_repo.insert(
        law_id=entry.law_id, new_serial_no=new_serial,
        old_serial_no=entry.last_serial_no or oldnew.old_version.serial_no,
        promulgation_no=(sr.promulgation_no if sr else "") or oldnew.new_version.promulgation_no,
        revision_type=(sr.revision_type if sr else "") or oldnew.new_version.revision_type,
        revision_reason=fulltext.revision_reason,
        enforce_date=enforce_date,
        unchanged_clauses=_unchanged_clauses_admrul(oldnew, located),
    )
    watchlist_repo.update_last_seen(entry.law_id, new_serial)

    success = sum(1 for _c, rs in located for r in rs if r.status.value == "성공")
    failed = sum(
        1 for _c, rs in located for r in rs
        if r.status.value in ("0건실패", "중복실패")
    )
    log.info(
        "행정규칙 %s(%s) 처리 완료: 저장 %d행, 위치확정 성공 %d/실패 %d",
        entry.official_name, entry.law_id, saved, success, failed,
    )
    return ProcessOutcome(result, diff_count=saved, located_success=success, located_failed=failed)


def process_entry(
    client: LawApiClient,
    version_repo: VersionRepo,
    watchlist_repo: WatchlistRepo,
    change_log_repo: ChangeLogRepo,
    article_diff_repo: ArticleDiffRepo,
    entry: WatchlistEntry,
) -> ProcessOutcome:
    """entry.law_type 에 따라 process_law_entry / process_admrul_entry 로 분기.

    워치리스트를 순회하는 배치/스크립트는 이 함수 하나만 부르면 되고,
    법령/행정규칙 구분을 직접 신경 쓰지 않아도 된다.
    """
    fn = process_admrul_entry if entry.law_type == ADMRUL_LAW_TYPE else process_law_entry
    return fn(client, version_repo, watchlist_repo, change_log_repo, article_diff_repo, entry)


def _strip_text_fields(obj):
    """★★ 실측 발견(2026-07-18, 전자정부법 제5조③): parse_articles()가
    만드는 ClauseNode.text/ItemNode.text/SubItemNode.text/ArticleUnit.content
    는 lawService 원문 그대로라 "<개정 2020.6.9>" 같은 각주가 그대로 섞여
    있다. article_diff.old_text에서 이미 한 번 같은 문제를 고쳤는데
    (strip_annotations 누락), 오늘 새로 추가한 law_articles_parsed/
    administrative_rule_articles_parsed 캐시 컬럼에서 똑같은 문제가
    재발했다 — 이번엔 다른 경로(parse_articles 자체 출력)라 그때 고친
    코드가 적용되지 않는 지점이었다. "text"/"content" 키만 대상으로
    재귀적으로 strip_annotations를 적용한다(라벨/마커/날짜 필드는 원래
    각주가 낄 일이 없으므로 건드리지 않는다).
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("text", "content") and isinstance(v, str):
                out[k] = strip_annotations(v).strip()
            else:
                out[k] = _strip_text_fields(v)
        return out
    if isinstance(obj, (list, tuple)):
        # ★ 실측 발견(2026-07-18): dataclasses.asdict()는 tuple 필드를
        # list가 아니라 tuple 그대로 남긴다(ClauseNode.items 등) —
        # list만 검사하면 이 중첩 tuple 안의 text/content가 하나도 안
        # 지워지는 채로 새어나간다. list/tuple 둘 다 처리한다.
        return [_strip_text_fields(x) for x in obj]
    return obj


def _serialize_articles(articles: list) -> list[dict]:
    """parse_articles() 결과(ArticleUnit 트리)를 DB 저장용 JSON-호환 dict로.

    ★ 요구사항("파싱된 것도 DB에 담아달라")에 따라 law_full_text(원본)와
    별도로 저장하는 캐시용. dataclasses.asdict()는 중첩된 ClauseNode/
    ItemNode/SubItemNode까지 재귀적으로 dict로 풀어준다. locate/매칭에
    쓰는 원본 ArticleUnit 객체는 건드리지 않고(주석 유지가 매칭 로직에
    영향 없다는 게 이미 검증돼 있음), DB에 쓸 딕셔너리로 변환한 *이후*에만
    각주를 제거한다.
    """
    return [_strip_text_fields(asdict(art)) for art in articles]


def _serialize_units(units: list) -> list[dict]:
    """parse_admrul_units() 결과(SearchUnit 평평한 목록)를 DB 저장용으로."""
    return [_strip_text_fields(asdict(u)) for u in units]


def _unchanged_clauses(
    articles: list, located: list[tuple],
) -> dict[str, list[str]]:
    """개정된 조문 중, 이번에 안 바뀐 항(現行 항)의 라벨만 뽑는다.

    ★ 법령 전용: ClauseNode.change_type ("개정"/"신설"/... 또는 없으면 "")
    은 법제처 API가 항마다 이미 매겨주는 공식 필드라, 별도 추론 없이
    "빈 문자열 = 이번 개정에서 안 바뀐 현행 항"으로 바로 읽을 수 있다.
    행정규칙은 이런 항 단위 태그 자체가 없는 평문이라(parse_admrul_units
    참고) 이 함수를 적용하지 않는다 — LLM팀에게 "확정 사실만" 준다는
    schema.py 설계 원칙상, 근거 없는 추정치를 섞고 싶지 않기 때문이다.

    ★★ 실측 발견(2026-07-16, 전자정부법 제2조): 항(①②③) 없이 호가
    조문에 바로 붙는 조문은 `parse_articles`가 라벨 없는 더미 ClauseNode
    (no="", change_type="")를 하나 만들어 호 목록을 담는 그릇으로 쓴다
    (flatten_searchable 의 "headerless item container" 처리와 동일한
    구조). 이 더미 항은 실제 "①②③" 같은 항이 전혀 아닌데, change_type이
    비어있다는 이유만으로 "안 바뀐 항"에 포함되면 `{"제2조": [""]}` 처럼
    빈 문자열 라벨이 그대로 노출되어 LLM팀 입장에서 의미를 알 수 없는
    항목이 된다. 라벨이 빈 clause는 애초에 "항"이 아니므로 제외한다.

    ★★★ 실측 발견(2026-07-18, 청소년복지 지원법 제16조의2 등): 조문
    자체가 신설되었거나(제16조의2) 크게 재구성된(제18조의4/5/6) 경우,
    법제처 API는 그 조문 안 "모든" 항의 항제개정유형을 통째로 비워둔다
    (None) — 일부만 개정된 조문(제31조의2, 제75조 등)에서는 바뀐 항에만
    값이 채워지고 안 바뀐 항은 진짜로 비어있는 것과 대조적이다. 이걸
    구분 안 하고 "비어있으면 무조건 현행유지"로 읽으면, 방금 change_type
    ="신설"로 저장한 항(예: 제16조의2①②)이 같은 조문 안에서 동시에
    "현행유지"로도 보고되는 자기모순이 생긴다({"제16조의2": ["①","②"]}
    가 articles 배열의 신설 항목과 정면으로 충돌). 판별 신호는 "그
    조문의 항 중 하나라도 change_type이 채워져 있는가" — 하나라도
    채워져 있으면 API가 이번 조문에 대해 항 단위 태깅을 실제로 하고
    있다는 뜻이므로 비어있는 항은 진짜 현행유지로 신뢰할 수 있고, 전부
    비어있으면 항 단위 태깅 자체가 생략된 것이므로 이 조문에 대해서는
    아무것도 "현행유지"라고 확정하지 않는다(모른다고 솔직히 비워둠 —
    틀린 확정 사실을 주는 것보다 안전).
    """
    touched_articles = {
        lr.unit.article_label
        for _c, results in located for lr in results
        if lr.status.value == "성공" and lr.unit is not None
    }
    if not touched_articles:
        return {}

    by_label = {art.label: art for art in articles}
    out: dict[str, list[str]] = {}
    for label in touched_articles:
        art = by_label.get(label)
        if art is None or not art.clauses:
            continue
        named_clauses = [c for c in art.clauses if c.no]
        if not any(c.change_type for c in named_clauses):
            continue  # 이 조문은 항 단위 개정유형 태깅 자체가 없음 — 확정 불가
        unchanged = [c.no for c in named_clauses if not c.change_type]
        if unchanged:
            out[label] = unchanged
    return out


def _unchanged_clauses_admrul(oldnew, located: list[tuple]) -> dict[str, list[str]]:
    """행정규칙 전용: oldAndNew의 "(생략)/(현행과 같음)" 스킵 표시에서 안
    바뀐 항/호 라벨을 뽑는다.

    ★ 설계(2026-07-18): law쪽 _unchanged_clauses()는 법제처가 항마다
    매겨주는 공식 항제개정유형 태그를 읽지만, admrul 본문은 평문이라 그
    태그 자체가 없다(parse_admrul_units 참고). 대신 신구법 비교 API가
    스킵 표시("1. ∼ 4. (생 략)")로 이미 "이 범위는 안 바뀌었다"를
    알려주고 있는데, extract_changes()는 지금까지 이걸 UNCHANGED로만
    분류하고 버려왔다(parse/oldnew.py). extract_admrul_unchanged()가 그
    버려지던 정보를 되살린다 — 근거는 여전히 법제처 API 자체이지 이
    코드의 추론이 아니다.

    touched_articles 필터는 law쪽과 동일한 이유: 이번에 실제로 CHANGED로
    감지된 조문에 대해서만 "안 바뀜"을 보고한다.
    """
    touched_articles = {
        lr.unit.article_label
        for _c, results in located for lr in results
        if lr.status.value == "성공" and lr.unit is not None
    }
    if not touched_articles:
        return {}
    return extract_admrul_unchanged(oldnew.old_texts, oldnew.new_texts, touched_articles)


def _parse_date(yyyymmdd: str) -> date | None:
    yyyymmdd = (yyyymmdd or "").strip()
    if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    try:
        return date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))
    except ValueError:
        return None