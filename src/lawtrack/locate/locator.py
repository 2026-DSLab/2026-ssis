"""조/항/호/목 위치 확정 — 6가드 파이프라인.

배경: oldAndNew 는 "무엇이 바뀌었는지"(<P> 태그)는 정확히 알려주지만,
그 변경이 정확히 몇 조 몇 항 몇 호에 있는지는 구조화된 필드로 주지
않는다(구조문목록/신조문목록은 조문 순번 "no" 만 가질 뿐, 항/호/목
깊이의 구조가 없다). 반면 lawService 본문은 조/항/호/목이 전부 태그로
분리되어 있다. 그래서 "oldAndNew 에서 나온 변경 문장을 lawService
전문 안에서 찾아 정확한 위치를 알아낸다"는 전략을 쓴다 — 이 파일이
그 검색을 담당한다.

6가드는 코드 전체에 흩어져 있지 않고 아래처럼 각 계층에 나뉘어 있다.

    가드①②(유니코드/공백 정규화) → text.normalize.count_occurrences 내부
    가드③(개정문 파트 제외)      → parse.fulltext.parse_articles 가
                                    애초에 개정문/제개정이유 키에 접근하지
                                    않으므로, 여기 넘어오는 SearchUnit 에는
                                    구조적으로 섞일 수 없음 (검증 완료)
    가드④(항 기호로 분해)        → text.split.searchable_fragments
    가드⑤(번호 결합 재검색)      → 이 파일의 _locate_fragment
    가드⑥(판정 + 실패 로그)      → 이 파일의 LocateResult / status

실측 근거:
    - 전자정부법: oldAndNew 블록을 통짜로 검색하면 실패, ①②③ 기호로
      쪼개야 검색됨.
    - 국고금관리법 시행령: 내용만 검색하면 중복 매칭, 호번호까지 붙이면
      해소됨.
    - 국민기초생활보장법: <P> 내용이 짧아 여러 곳에 중복 매칭됨.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from lawtrack.parse.fulltext import SearchUnit
from lawtrack.parse.oldnew import ArticleChange, ChangeType
from lawtrack.text.normalize import count_occurrences
from lawtrack.text.split import (
    Fragment,
    Level,
    searchable_fragments,
    strip_annotations,
    strip_article_head,
)

log = logging.getLogger(__name__)


class LocateStatus(str, Enum):
    SUCCESS = "성공"
    ZERO_MATCH = "0건실패"
    DUPLICATE = "중복실패"
    DELETED_SKIP = "삭제(위치탐색제외)"


@dataclass(frozen=True)
class LocateResult:
    """조각 하나에 대한 위치 확정 결과.

    tried 는 사람이 읽을 수 있는 진행 로그다. 실패 시 "어느 가드까지
    가서 왜 깨졌는지"가 여기 남아야 개선이 가능하다 — 조용히 넘기지
    않는다는 원칙의 구현.
    """

    status: LocateStatus
    unit: SearchUnit | None
    fragment: Fragment | None
    match_count: int
    tried: tuple[str, ...] = field(default_factory=tuple)

    @property
    def location_label(self) -> str | None:
        return self.unit.location_label if self.unit else None


def _count_matches(needle: str, units: list[SearchUnit]) -> list[tuple[SearchUnit, int]]:
    """조각 텍스트가 각 유닛에 몇 번 등장하는지.

    count_occurrences 내부에서 유니코드(가드①)·공백(가드②) 정규화가
    이미 적용된다.
    """
    if not needle.strip():
        return []
    out = []
    for u in units:
        if not u.text:
            continue
        c = count_occurrences(u.text, needle)
        if c:
            out.append((u, c))
    return out


def _total(matches: list[tuple[SearchUnit, int]]) -> int:
    return sum(c for _, c in matches)


def _locate_fragment(frag: Fragment, units: list[SearchUnit]) -> LocateResult:
    """가드④(이미 분해된 조각) 이후 → 가드⑤⑥ 적용."""
    tried: list[str] = []
    body = frag.text.strip()
    tried.append(f"본문검색: '{body[:24]}{'…' if len(body) > 24 else ''}'")

    matches = _count_matches(body, units)
    total = _total(matches)

    if total == 1:
        return LocateResult(LocateStatus.SUCCESS, matches[0][0], frag, 1, tuple(tried))

    if total == 0:
        tried.append("0건 (정규화 적용된 상태에서도 미발견)")
        return LocateResult(LocateStatus.ZERO_MATCH, None, frag, 0, tuple(tried))

    # total >= 2 → 가드⑤: 번호(마커) 결합 재검색
    if frag.marker:
        combined = f"{frag.marker} {body}".strip()
        tried.append(f"중복 {total}건 → 번호결합 재검색: '{combined[:24]}…'")
        matches2 = _count_matches(combined, units)
        total2 = _total(matches2)
        if total2 == 1:
            return LocateResult(LocateStatus.SUCCESS, matches2[0][0], frag, 1, tuple(tried))
        if total2 == 0:
            tried.append("번호결합 재검색이 0건으로 악화 → 원 중복 상태로 실패 처리")
        else:
            tried.append(f"번호결합 후에도 중복 {total2}건")
    else:
        tried.append(f"중복 {total}건, 마커 없어 번호결합 불가")

    return LocateResult(LocateStatus.DUPLICATE, None, frag, total, tuple(tried))


def locate_change(change: ArticleChange, units: list[SearchUnit]) -> list[LocateResult]:
    """ArticleChange 하나(구/신 한 쌍)를 조각내어 위치를 확정한다.

    반환값이 list 인 이유: oldAndNew 의 한 블록(예: "no"=1)이 실제로는
    여러 항/호를 한 문자열로 담고 있을 수 있어(실측: 전자정부법), 조각
    개수만큼 결과가 나온다.

    change_type=DELETED 는 검색하지 않는다: 삭제된 내용은 정의상 현재
    (신) 본문에 더 이상 존재하지 않으므로, 신 전문에서 찾으려는 시도
    자체가 무의미하다. 위치 없이 "삭제됨"으로만 기록한다.
    """
    if change.change_type is ChangeType.DELETED:
        return [
            LocateResult(
                LocateStatus.DELETED_SKIP, None, None, 0,
                ("삭제된 항목은 현행 전문에 존재하지 않음 — 위치탐색 생략",),
            )
        ]

    search_text = strip_annotations(strip_article_head(change.new_clean))
    fragments = searchable_fragments(search_text)
    if not fragments:
        # 항/호 기호가 없는 단문 — 전체를 하나의 조각으로 취급.
        fragments = [Fragment(Level.NONE, None, search_text.strip(), search_text.strip())]

    return [_locate_fragment(f, units) for f in fragments]


def locate_all(
    changes: list[ArticleChange], units: list[SearchUnit]
) -> list[tuple[ArticleChange, list[LocateResult]]]:
    """여러 ArticleChange 를 일괄 처리. 실패 건은 요약 로그를 남긴다."""
    out = []
    for change in changes:
        results = locate_change(change, units)
        out.append((change, results))

        fails = [r for r in results if r.status in (LocateStatus.ZERO_MATCH, LocateStatus.DUPLICATE)]
        if fails:
            log.warning(
                "위치확정 실패 %d/%d건 (change index=%d, type=%s): %s",
                len(fails), len(results), change.index, change.change_type.value,
                [(f.status.value, f.tried) for f in fails],
            )
    return out


def summarize(results: list[tuple[ArticleChange, list[LocateResult]]]) -> dict[str, int]:
    """상태별 집계. 운영 모니터링/보고용."""
    counts: dict[str, int] = {}
    for _change, locate_results in results:
        for r in locate_results:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
    return counts