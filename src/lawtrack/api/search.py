"""목록조회 (target=law / target=admrul).

이 모듈의 책임은 "이름으로 검색해서, 그중 정확히 일치하는 것을 고른다"
까지다. 등록 여부 판단(폐지/통합/시행전 분기)은 호출부(워치리스트 관리
로직)의 책임으로 남긴다 — 여기서는 "0건이었다"는 사실만 정직하게
돌려준다.

실측된 함정과 대응:
    - query 는 부분일치 → 유사법 다수 혼입 (지방자치단체~, 법원~ 등)
      => 정규화 후 완전일치로 재필터링 (names_match)
    - 검색어가 짧으면 정답이 display 밖으로 밀림 (에너지법: 32건 중 17번째)
      => client.search() 가 display=100 을 강제하므로 여기서는 대응 불필요.
         단, totalCnt > display 이면 경고 로그를 남긴다.
    - 소관부처가 다르면 이름이 겹쳐도 다른 규칙
      (예: 협상에 의한 계약체결기준 — 재정경제부 vs 방위사업청)
      => dept_code 필터를 선택 인자로 제공.
    - admrul 은 단일 결과가 dict 로 온다(87%) → as_list 로 정규화.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from lawtrack.api.client import ApiResponse, LawApiClient, LawApiFormatError
from lawtrack.parse.jsonutil import as_list, dig_list, text_of
from lawtrack.text.normalize import names_match

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 결과 타입
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LawSearchResult:
    """법령 목록조회 1건."""

    law_id: str
    serial_no: str          # 법령일련번호(MST)
    law_name: str
    law_abbr: str
    law_type: str            # 법령구분명 (법률/대통령령/부령 등)
    promulgation_date: str
    promulgation_no: str
    revision_type: str       # 제개정구분명
    dept_codes: tuple[str, ...]
    dept_names: tuple[str, ...]
    enforce_date: str
    detail_link: str


@dataclass(frozen=True)
class AdmrulSearchResult:
    """행정규칙 목록조회 1건."""

    rule_id: str
    serial_no: str            # 행정규칙일련번호
    rule_name: str
    rule_kind: str             # 행정규칙종류 (고시/훈령/예규)
    dept_name: str
    promulgation_date: str     # 발령일자
    promulgation_no: str       # 발령번호 ("2024-53" 형식, 법령과 다름)
    revision_type: str
    detail_link: str


# ---------------------------------------------------------------------------
# 목록조회 - 필드 후보 경로
# ---------------------------------------------------------------------------
# 루트 구조를 실측(JSON)으로 확정하지 못했으므로, 항목 리스트를 여러 후보
# 경로로 시도한다. 실제 응답 확인 후 첫 번째 성공 경로로 좁혀도 된다.

_LAW_ITEM_PATHS: tuple[tuple[str, ...], ...] = (
    ("LawSearch", "law"),
    ("law",),
)

_ADMRUL_ITEM_PATHS: tuple[tuple[str, ...], ...] = (
    ("AdmRulSearch", "admrul"),
    ("admrul",),
)


def _first_nonempty(data: dict, paths: tuple[tuple[str, ...], ...]) -> list:
    for path in paths:
        items = dig_list(data, *path)
        if items:
            return items
    return []


def _dept_tuple(raw: str) -> tuple[str, ...]:
    """소관부처 콤마 다중값 분리.

    실측: 국가계약법 시행규칙 = "1053000,1230000" (재정경제부,조달청)
    """
    raw = text_of(raw)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# ---------------------------------------------------------------------------
# 법령 검색
# ---------------------------------------------------------------------------

def search_law(client: LawApiClient, query: str) -> list[LawSearchResult]:
    """법령명으로 검색. 부분일치 결과를 그대로 반환한다 (필터링은 상위 책임)."""
    resp = client.search(target="law", query=query)
    data = resp.json_or_raise()

    total = text_of(_dig_total_count(data))
    items = _first_nonempty(data, _LAW_ITEM_PATHS)

    if total and items and int(total) > len(items):
        log.warning(
            "검색결과 잘림 위험: query=%r totalCnt=%s 수신=%d건 "
            "(display 설정 확인 필요)",
            query, total, len(items),
        )

    results = []
    for raw in items:
        results.append(
            LawSearchResult(
                law_id=text_of(raw.get("법령ID")),
                serial_no=text_of(raw.get("법령일련번호")),
                law_name=text_of(raw.get("법령명한글")),
                law_abbr=text_of(raw.get("법령약칭명")),
                law_type=text_of(raw.get("법령구분명")),
                promulgation_date=text_of(raw.get("공포일자")),
                promulgation_no=text_of(raw.get("공포번호")),
                revision_type=text_of(raw.get("제개정구분명")),
                dept_codes=_dept_tuple(raw.get("소관부처코드", "")),
                dept_names=tuple(
                    p.strip() for p in text_of(raw.get("소관부처명")).split(",") if p.strip()
                ),
                enforce_date=text_of(raw.get("시행일자")),
                detail_link=text_of(raw.get("법령상세링크")),
            )
        )
    return results


def search_admrul(client: LawApiClient, query: str) -> list[AdmrulSearchResult]:
    """행정규칙명으로 검색."""
    resp = client.search(target="admrul", query=query)
    data = resp.json_or_raise()

    total = text_of(_dig_total_count(data))
    items = _first_nonempty(data, _ADMRUL_ITEM_PATHS)

    if total and items and int(total) > len(items):
        log.warning(
            "검색결과 잘림 위험(admrul): query=%r totalCnt=%s 수신=%d건",
            query, total, len(items),
        )

    results = []
    for raw in items:
        results.append(
            AdmrulSearchResult(
                rule_id=text_of(raw.get("행정규칙ID")),
                serial_no=text_of(raw.get("행정규칙일련번호")),
                rule_name=text_of(raw.get("행정규칙명")),
                rule_kind=text_of(raw.get("행정규칙종류")),
                dept_name=text_of(raw.get("소관부처명")),
                promulgation_date=text_of(raw.get("발령일자")),
                promulgation_no=text_of(raw.get("발령번호")),
                revision_type=text_of(raw.get("제개정구분명")),
                detail_link=text_of(raw.get("행정규칙상세링크")),
            )
        )
    return results


def _dig_total_count(data: dict) -> str:
    for path in (("LawSearch", "totalCnt"), ("totalCnt",),
                 ("AdmRulSearch", "totalCnt")):
        cur = data
        ok = True
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if ok:
            return cur
    return ""


# ---------------------------------------------------------------------------
# 완전일치 해석 (워치리스트 등록용)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolveOutcome:
    """이름 → ID 해석 결과.

    status:
        "matched"    정확히 1건으로 확정
        "ambiguous"  정규화 후에도 여러 건 (소관부처로 좁혀야 함)
        "not_found"  0건 — 폐지/통합/시행전/오탈자 등, 호출부가 분기 판단
    """

    status: str
    candidates: list  # LawSearchResult | AdmrulSearchResult


def resolve_law(
    client: LawApiClient, query: str, *, dept_code: str | None = None
) -> ResolveOutcome:
    """법령명 → 정확히 일치하는 항목 해석.

    실측 사례 대응:
        - "소프트웨어산업 진흥법" 검색 시 정확매칭 0건 (제명변경, not_found)
        - "국가를 당사자로 하는 계약에 관한 법률" 검색 시 유사법 7건 혼입
          → names_match 완전일치로 자동 제거됨 (지방자치단체~ 등은 이름이
            다르므로 정규화해도 일치하지 않는다)
    """
    all_results = search_law(client, query)
    exact = [r for r in all_results if names_match(r.law_name, query)]

    if dept_code:
        narrowed = [r for r in exact if dept_code in r.dept_codes]
        if narrowed:
            exact = narrowed

    if len(exact) == 1:
        return ResolveOutcome("matched", exact)
    if len(exact) > 1:
        log.warning(
            "법령명 '%s' 정규화 완전일치가 %d건 — 소관부처 조건 필요: %s",
            query, len(exact), [r.law_id for r in exact],
        )
        return ResolveOutcome("ambiguous", exact)
    return ResolveOutcome("not_found", all_results)


def resolve_admrul(
    client: LawApiClient, query: str, *, dept_name: str | None = None
) -> ResolveOutcome:
    """행정규칙명 → 정확히 일치하는 항목 해석.

    실측: '협상에 의한 계약체결기준' totalCnt=3, 소관부처 다르면 무관 규칙.
    """
    all_results = search_admrul(client, query)
    exact = [r for r in all_results if names_match(r.rule_name, query)]

    if dept_name:
        narrowed = [r for r in exact if r.dept_name == dept_name]
        if narrowed:
            exact = narrowed

    if len(exact) == 1:
        return ResolveOutcome("matched", exact)
    if len(exact) > 1:
        log.warning(
            "행정규칙명 '%s' 완전일치가 %d건 — 소관부처 조건 필요: %s",
            query, len(exact), [r.rule_id for r in exact],
        )
        return ResolveOutcome("ambiguous", exact)
    return ResolveOutcome("not_found", all_results)