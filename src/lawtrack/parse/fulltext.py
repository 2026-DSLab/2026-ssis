"""전문(lawService 본문) → 조/항/호/목 검색 트리.

★ 가드 ③ (개정문 이중매칭 방지)와 직결된다.

    실측: 신설 조문은 <개정문>(관보 공포문)과 <조문>(현행 법전) 양쪽에
    텍스트가 100% 동일하게 들어간다 (아동복지법 MST=281929 실측).
    이 모듈은 api/fulltext.py 가 이미 분리해 둔 '조문' 노드만 다루고,
    '개정문'/'제개정이유' 쪽에는 아예 접근하지 않는다 — 즉 이 모듈이
    만들어내는 검색 트리 자체에 개정문 텍스트가 섞여 들어올 수 없다.
    (분리 자체는 api/fulltext.FullTextResult.revision_text 가 담당)

필드명 신뢰도:
    조문번호 / 조문변경여부 / 항번호 / 항내용 / 항제개정유형 /
    항제개정일자문자열 / 호번호 / 호가지번호 / 호내용
        → ✅ 실측 확인된 필드명 (국민기초생활보장법 시행규칙,
           아동복지법 응답에서 직접 확인)

    목(目) 레벨 필드명(목번호/목내용 등)
        → ❓ 미확인. 호와 동일한 패턴으로 추정해 구현했으나,
           실제 응답 확인 후 조정 필요.

    상위 경로("조문" 밑에 바로 항목 리스트가 오는지, "조문단위"라는
    한 겹이 더 있는지)
        → ❓ JSON 루트 구조 미확정으로 후보 경로 탐색 방식 사용.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from lawtrack.parse.jsonutil import as_list, dig, find_key, text_of
from lawtrack.text.split import ArticleNo, article_title, strip_article_head

log = logging.getLogger(__name__)

_ARTICLE_LIST_PATHS: tuple[tuple[str, ...], ...] = (
    ("조문", "조문단위"),
    ("조문",),
)


@dataclass(frozen=True)
class SubItemNode:  # 목 — ❓ 필드명 미확인, 호와 동일 패턴으로 추정
    label: str
    text: str


@dataclass(frozen=True)
class ItemNode:  # 호
    no: str          # 호번호 (예: "12.")
    branch: str       # 호가지번호 (예: "2" → 12의2)
    text: str
    subitems: tuple[SubItemNode, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        if self.branch:
            return f"{self.no.rstrip('.')}의{self.branch}"
        return self.no


@dataclass(frozen=True)
class ClauseNode:  # 항
    no: str                # 항번호 (예: "①")
    text: str               # 항내용
    change_type: str        # 항제개정유형 ("개정"/"신설"/... 없으면 "")
    change_dates: str       # 항제개정일자문자열
    items: tuple[ItemNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ArticleUnit:  # 조
    code: str               # 조문번호 원문값 (예: "6") — 실측 확인: 순수 조번호만, 가지번호 별도
    branch: str               # 조문가지번호 (예: "2" → 제6조의2) — 실측 확인된 필드
    label: str               # "제6조의2" 형태로 정규화된 표시용 라벨
    title: str | None          # 짧은 괄호 제목 (예: "기준 중위소득의 산정")
    changed: bool             # 조문변경여부 Y/N
    content: str = ""          # ★ "조문내용" 원문 그대로.
    # 항이 있는 조문은 이 필드가 헤더뿐이지만("제6조의2(기준 중위소득의 산정)"),
    # 항이 없는 단일 문단형 조문(예: 제56조의6)은 본문 전체가 여기 들어있다.
    # (실측: 항 배열 없는 조문에서 title만 쓰면 본문이 통째로 유실되는
    #  버그가 있었음 — 이 필드가 그 수정)
    enforce_date: str = ""    # 조문시행일자 (예: "20251001") — 실측 확인, 조문 단위 시행일
    clauses: tuple[ClauseNode, ...] = field(default_factory=tuple)

    @property
    def full_text(self) -> str:
        """조문 전체를 이어붙인 텍스트. 항이 없으면 조문 자체 텍스트만."""
        if not self.clauses:
            return self.title or ""
        return " ".join(c.text for c in self.clauses if c.text)


@dataclass(frozen=True)
class SearchUnit:
    """locate 단계가 실제로 순회하며 검색할 최소 단위.

    location_label 은 진단/보고용 ("제39조③", "제10조의2②12의2." 등).
    """

    article_code: str
    article_label: str
    clause_no: str
    item_label: str
    subitem_label: str
    text: str
    changed: bool
    enforce_date: str = ""  # ✅ 실측: "조문시행일자" — 사회보장기본법 제21조처럼
    # 같은 조문이 즉시시행/유예시행으로 나뉘는 경우를 위한 유니크키 구성요소

    @property
    def location_label(self) -> str:
        parts = [self.article_label]
        if self.clause_no:
            parts.append(self.clause_no)
        if self.item_label:
            parts.append(self.item_label)
        if self.subitem_label:
            parts.append(self.subitem_label)
        return "".join(parts)


def _article_label(code: str, branch: str) -> str:
    """조문번호 + 조문가지번호 → 표시 라벨.

    ✅ 실측 확인 (2026-07-16, 국민기초생활보장법 MST=276653 원본 JSON):
        "조문번호": "6", "조문가지번호": "2" → 제6조의2

    이전 버전은 조문번호가 lsJoHstInf 의 6자리 인코딩("000602")과 같을
    거라 가정했는데, 실제 lawService 본문조회 응답은 조번호와 가지번호가
    "6" / "2" 처럼 완전히 분리된 별도 필드였다. 그 가정으로 짰던 코드가
    가지번호를 통째로 누락시켜 "제6조의2"가 "제6조"로 잘못 표시되는
    버그가 있었다 — 이 함수가 그 수정본이다.
    """
    code = (code or "").strip()
    branch = (branch or "").strip().lstrip("0") or ("0" if branch and branch.strip("0") == "" else branch.strip())
    if not code:
        return "(조번호 미상)"
    label = f"제{code}조"
    if branch and branch != "0":
        label += f"의{branch}"
    return label


def _find_article_items(root: dict) -> list:
    for path in _ARTICLE_LIST_PATHS:
        items = dig(root, *path)
        if items:
            return as_list(items)
    # 경로 후보가 전부 실패하면 트리 전체에서 '조문번호' 를 가진 dict 를 찾는다.
    found = find_key(root, "조문번호")
    if found is not None:
        log.debug("조문 목록 경로 탐색 실패 — find_key 로 대체 탐색됨")
    return []


def parse_articles(raw: dict) -> list[ArticleUnit]:
    """법령 본문 JSON(raw) → ArticleUnit 목록.

    raw 는 api/fulltext.FullTextResult.raw 를 그대로 받는다 (root 탐색은
    이 함수 내부에서 수행).
    """
    root = _find_root(raw)
    article_items = _find_article_items(root)
    if not article_items:
        log.warning("조문 목록을 찾지 못함 — 루트 구조 확인 필요 (keys=%s)", list(root)[:10])
        return []

    units = []
    for raw_article in article_items:
        if not isinstance(raw_article, dict):
            continue
        code = text_of(raw_article.get("조문번호"))
        branch = text_of(raw_article.get("조문가지번호"))
        # ✅ 실측: 조문 제목은 "조문제목" 필드에 이미 분리되어 있음
        #    (기존엔 "조문내용"에서 정규식으로 다시 뽑으려 했었는데,
        #     "조문내용"은 "제6조의2(기준 중위소득의 산정)"처럼 헤더 전체이고
        #     "조문제목"이 그 안의 괄호 안 텍스트만 이미 담고 있다.)
        title = text_of(raw_article.get("조문제목")) or article_title(text_of(raw_article.get("조문내용")))
        content = text_of(raw_article.get("조문내용"))
        changed = text_of(raw_article.get("조문변경여부")).upper() == "Y"
        enforce_date = text_of(raw_article.get("조문시행일자"))

        clauses = tuple(_parse_clause(c) for c in as_list(raw_article.get("항")))

        units.append(
            ArticleUnit(
                code=code,
                branch=branch,
                label=_article_label(code, branch),
                content=content,
                title=title,
                changed=changed,
                enforce_date=enforce_date,
                clauses=clauses,
            )
        )
    return units


def _parse_clause(raw_clause: dict) -> ClauseNode:
    if not isinstance(raw_clause, dict):
        raw_clause = {}
    items = tuple(_parse_item(i) for i in as_list(raw_clause.get("호")))
    return ClauseNode(
        no=text_of(raw_clause.get("항번호")),
        text=text_of(raw_clause.get("항내용")),
        change_type=text_of(raw_clause.get("항제개정유형")),
        change_dates=text_of(raw_clause.get("항제개정일자문자열")),
        items=items,
    )


def _parse_item(raw_item: dict) -> ItemNode:
    if not isinstance(raw_item, dict):
        raw_item = {}
    # 목(目) 필드명 미확인 — "목" 키 자체의 존재 여부부터 방어적으로 확인.
    subitems = tuple(_parse_subitem(s) for s in as_list(raw_item.get("목")))
    return ItemNode(
        no=text_of(raw_item.get("호번호")),
        branch=text_of(raw_item.get("호가지번호")),
        text=text_of(raw_item.get("호내용")),
        subitems=subitems,
    )


def _parse_subitem(raw_sub) -> SubItemNode:  # ❓ 필드명 미확인, 호 패턴 추정
    if not isinstance(raw_sub, dict):
        return SubItemNode(label="", text=text_of(raw_sub))
    label = text_of(raw_sub.get("목번호") or raw_sub.get("목가지번호"))
    text = text_of(raw_sub.get("목내용"))
    return SubItemNode(label=label, text=text)


def _find_root(raw: dict) -> dict:
    for key in ("법령", "Law", "LawService"):
        if key in raw and isinstance(raw[key], dict):
            return raw[key]
    return raw


# ---------------------------------------------------------------------------
# 검색 트리 평탄화
# ---------------------------------------------------------------------------

def flatten_searchable(articles: list[ArticleUnit]) -> list[SearchUnit]:
    """locate 단계가 순회할 최소 검색 단위 리스트.

    항이 없는 조문(단문 조문)은 조문 자체를 하나의 유닛으로 반환한다.
    enforce_date는 조문(art) 단위 값을 그대로 물려받는다 — 실측 확인된
    "조문시행일자"가 항/호 레벨에 별도로 있지는 않았으므로, 조문 레벨
    값을 그 조문 산하 모든 유닛에 공통 적용한다.
    """
    units: list[SearchUnit] = []
    for art in articles:
        if not art.clauses:
            # ★ 수정: 항이 없는 단일 문단형 조문은 title(짧은 괄호 제목)이
            # 아니라 content(조문내용 전체)에서 헤더만 뗀 본문을 써야 한다.
            # 실측: 제56조의6(항 없음)에서 title만 쓰면 본문이 통째로
            # 유실되어 검색이 항상 0건실패로 죽는 버그가 있었다.
            body = strip_article_head(art.content) if art.content else (art.title or "")
            units.append(
                SearchUnit(
                    article_code=art.code, article_label=art.label,
                    clause_no="", item_label="", subitem_label="",
                    text=body, changed=art.changed,
                    enforce_date=art.enforce_date,
                )
            )
            continue
        for clause in art.clauses:
            if not clause.items:
                units.append(
                    SearchUnit(
                        article_code=art.code, article_label=art.label,
                        clause_no=clause.no, item_label="", subitem_label="",
                        text=clause.text, changed=art.changed,
                        enforce_date=art.enforce_date,
                    )
                )
                continue
            for item in clause.items:
                if not item.subitems:
                    units.append(
                        SearchUnit(
                            article_code=art.code, article_label=art.label,
                            clause_no=clause.no, item_label=item.label, subitem_label="",
                            text=item.text, changed=art.changed,
                            enforce_date=art.enforce_date,
                        )
                    )
                    continue
                for sub in item.subitems:
                    units.append(
                        SearchUnit(
                            article_code=art.code, article_label=art.label,
                            clause_no=clause.no, item_label=item.label,
                            subitem_label=sub.label,
                            text=sub.text, changed=art.changed,
                            enforce_date=art.enforce_date,
                        )
                    )
    return units


def changed_articles(articles: list[ArticleUnit]) -> list[ArticleUnit]:
    """조문변경여부=Y 인 것만. 법령 1차 스크리닝(가드 이전 단계)에 사용.

    실측 정확도: 전자정부법 8/8, 국가계약법 1/1, 사회보장기본법시행령 3/3
    (세 가지 서로 다른 개정 유형에서 100% 일치 확인됨)
    """
    return [a for a in articles if a.changed]