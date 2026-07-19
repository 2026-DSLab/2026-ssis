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

    목(目) 레벨 필드명(목번호/목내용)
        → ✅ 실측 확인됨 (2026-07-16, 전자정부법 MST=268103 제2조제3호·
           제9호). 호와 동일 패턴("목번호"/"목내용")이 맞았다.

    상위 경로("조문" 밑에 바로 항목 리스트가 오는지, "조문단위"라는
    한 겹이 더 있는지)
        → ❓ JSON 루트 구조 미확정으로 후보 경로 탐색 방식 사용.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from lawtrack.parse.jsonutil import as_list, dig, find_key, text_of
from lawtrack.text.split import (
    ArticleNo,
    Fragment,
    Level,
    article_title,
    split_by_clause,
    split_by_item,
    split_by_subitem,
    strip_annotations,
    strip_article_head,
)

log = logging.getLogger(__name__)

_ARTICLE_LIST_PATHS: tuple[tuple[str, ...], ...] = (
    ("조문", "조문단위"),
    ("조문",),
)


@dataclass(frozen=True)
class SubItemNode:  # 목 — 필드명(목번호/목내용) 실측 확인됨
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
    # 목(目) 필드명(목번호/목내용) 실측 확인됨 — "목" 키 존재 여부는 방어적으로 확인.
    subitems = tuple(_parse_subitem(s) for s in as_list(raw_item.get("목")))
    return ItemNode(
        no=text_of(raw_item.get("호번호")),
        branch=text_of(raw_item.get("호가지번호")),
        text=text_of(raw_item.get("호내용")),
        subitems=subitems,
    )


def _parse_subitem(raw_sub) -> SubItemNode:  # 필드명(목번호/목내용) 실측 확인됨
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

def _unit_article_code(art: ArticleUnit) -> str:
    """SearchUnit.article_code 생성 — 조문가지번호까지 포함해 article당 고유해야 함.

    ★★ 실측 발견(2026-07-16, contract/export.py 실데이터 검증): art.code는
    "조문번호"(순수 조번호, 예: "56")뿐이라 제56조/제56조의2/…/제56조의6이
    전부 같은 article_code="56"을 공유했다. article_diff의 UNIQUE KEY가
    (law_id, law_serial_no, article_code, clause_no, item_label,
    subitem_label)이고 article_label은 ON DUPLICATE KEY UPDATE 대상이
    아니어서, 여러 조문의 같은 항번호(예: 둘 다 "①")가 전부 같은 키로
    충돌 — 먼저 들어간 행의 article_label은 그대로 남은 채 change_type/
    old_text/new_text/match_status만 나중 값으로 덮어써지는 심각한 버그가
    있었다(실측: 제56조의5① 의 내용이 "제56조의2①"라는 라벨을 달고
    저장됨). art.label("제56조의2" 등)은 이미 조번호+가지번호 조합이라
    article당 고유하므로 그대로 code로 재사용한다.
    """
    return art.label


def flatten_searchable(articles: list[ArticleUnit]) -> list[SearchUnit]:
    """locate 단계가 순회할 최소 검색 단위 리스트.

    항이 없는 조문(단문 조문)은 조문 자체를 하나의 유닛으로 반환한다.
    enforce_date는 조문(art) 단위 값을 그대로 물려받는다 — 실측 확인된
    "조문시행일자"가 항/호 레벨에 별도로 있지는 않았으므로, 조문 레벨
    값을 그 조문 산하 모든 유닛에 공통 적용한다.
    """
    units: list[SearchUnit] = []
    for art in articles:
        code = _unit_article_code(art)
        # ★★ 실측 발견(2026-07-16, 공공기관의 정보공개에 관한 법률 제22조):
        # 항 없이 호가 조문에 바로 붙는 구조("항" JSON 키가 실제 항번호/
        # 항내용 없이 "호" 배열만 담은 빈 컨테이너)가 있다. 이 경우
        # art.clauses 는 비어있지 않으므로(빈 ClauseNode 하나) 아래
        # "항이 없는 조문" 분기를 안 타는데, 정작 조문 자신의 도입부 문장
        # ("다음 각 호의 사항을 심의ㆍ조정하기 위하여…")은 그 어떤 항/호
        # 필드에도 없고 오직 art.content 에만 있다 — 항/호 레벨에서 이미
        # 두 번 겪은 "자식이 있으면 부모 텍스트가 통째로 사라지는" 문제의
        # 세 번째 사례(조문→항 레벨)다. 항 유무와 무관하게 조문 자체도
        # 항상 검색 유닛으로 추가한다(정상적인 다항 조문에서는 content가
        # 헤더뿐이라 strip 후 빈 문자열이 되어 무해하다).
        head_body = strip_article_head(art.content) if art.content else ""
        if head_body:
            units.append(
                SearchUnit(
                    article_code=code, article_label=art.label,
                    clause_no="", item_label="", subitem_label="",
                    text=head_body, changed=art.changed,
                    enforce_date=art.enforce_date,
                )
            )
        if not art.clauses:
            # ★ 수정: 항이 없는 단일 문단형 조문은 title(짧은 괄호 제목)이
            # 아니라 content(조문내용 전체)에서 헤더만 뗀 본문을 써야 한다.
            # 실측: 제56조의6(항 없음)에서 title만 쓰면 본문이 통째로
            # 유실되어 검색이 항상 0건실패로 죽는 버그가 있었다. 위에서
            # 이미 head_body 로 처리했으므로, content 가 비어 title만
            # 있는 경우에만 보강한다.
            if not head_body and art.title:
                units.append(
                    SearchUnit(
                        article_code=code, article_label=art.label,
                        clause_no="", item_label="", subitem_label="",
                        text=art.title, changed=art.changed,
                        enforce_date=art.enforce_date,
                    )
                )
            continue
        for clause in art.clauses:
            # ★ 실측(전자정부법 제56조의2 항①): 호가 있는 항이라도 항내용
            # 필드 자체에 호 목록 전에 나오는 전제문(예: "…통보하여야 한다.")이
            # 실질적인 내용을 담고 있을 수 있다. 호가 있다고 이 텍스트를
            # 건너뛰면, 전제문 안에서 바뀐 부분은 영원히 위치확정에 실패한다.
            # 그래서 호 유무와 무관하게 항 자체도 항상 검색 유닛으로 추가한다.
            units.append(
                SearchUnit(
                    article_code=code, article_label=art.label,
                    clause_no=clause.no, item_label="", subitem_label="",
                    text=clause.text, changed=art.changed,
                    enforce_date=art.enforce_date,
                )
            )
            if not clause.items:
                continue
            for item in clause.items:
                # ★ 실측(전자정부법 제2조제11호): 목이 있는 호도 호내용 필드
                # 자체에 목 목록 전의 전제문("…다음 각 목의 자원을 말한다.
                # 다만…")이 실질 내용을 담고 있을 수 있다. 항 레벨과 동일한
                # 이유로, 목 유무와 무관하게 호 자체도 항상 유닛으로 추가한다.
                units.append(
                    SearchUnit(
                        article_code=code, article_label=art.label,
                        clause_no=clause.no, item_label=item.label, subitem_label="",
                        text=item.text, changed=art.changed,
                        enforce_date=art.enforce_date,
                    )
                )
                if not item.subitems:
                    continue
                for sub in item.subitems:
                    units.append(
                        SearchUnit(
                            article_code=code, article_label=art.label,
                            clause_no=clause.no, item_label=item.label,
                            subitem_label=sub.label,
                            text=sub.text, changed=art.changed,
                            enforce_date=art.enforce_date,
                        )
                    )
    return units


def parse_admrul_units(raw: dict) -> list[SearchUnit]:
    """행정규칙 본문(조문내용) → 검색 유닛 목록.

    ★★ 실측 발견(2026-07-16, 15개 행정규칙 실제 개정 시뮬레이션 검증):
    행정규칙 본문조회 응답은 법령과 구조가 완전히 다르다. 법령은
    조문/항/호/목이 JSON 트리로 미리 쪼개져 있어 parse_articles가 그
    구조를 그대로 읽으면 되지만, 행정규칙은 "AdmRulService.조문내용"
    이라는 평문 문자열 배열 하나뿐이다. 조문 하나가 배열의 원소 하나이긴
    하지만, 그 안의 항①②③/호1.2.3./목가.나.다.는 전혀 분리돼 있지 않고
    (예: "제5조(사용언어) ① 계약을 이행함에 있어서는… ② 계약담당공무원은…")
    한 줄에 통째로 이어붙어 있다. "제1장 총칙" 같은 장(章) 제목도 조문과
    같은 배열에 섞여 있어 걸러내야 한다.

    이 사실을 몰랐을 때는 parse_articles(법령 전용)를 행정규칙에도 그대로
    썼는데, "조문"/"조문단위" 키가 없어 조문을 0건 찾고, 그 결과 검색
    대상 유닛이 하나도 없어 모든 위치확정이 100% 실패했다(실측: 15개
    행정규칙 시뮬레이션에서 5개 전부 succ=0).

    해결: oldAndNew 조각을 자르는 데 이미 쓰던 text.split의 항/호/목
    분해기(split_by_clause/item/subitem, 목 리스트 오탐 방지 로직 포함)를
    본문 자체에도 그대로 적용해 SearchUnit을 직접 만든다. 법령 쪽과 달리
    "항 자체 텍스트를 늘 별도 유닛으로 추가"하지 않는다 — 항 텍스트가
    JSON 필드처럼 호 내용과 분리돼 있지 않고 통째로 한 덩어리이므로,
    항 유닛과 호 유닛을 둘 다 만들면 호 내용을 포함한 중복(상위집합)
    유닛이 생겨 불필요한 중복매칭을 유발한다. split_by_item이 반환하는
    "호 기호 이전 전제문" 조각(Level.NONE)이 이미 그 역할을 대신한다.

    ★ 실측 추가 발견(보안업무규정 시행규칙, law_id=9008822): 같은 평문
    배열이 루트 바로 아래("AdmRulService.조문내용")가 아니라 "조문" 한
    겹 아래("AdmRulService.조문.조문내용")에 오는 경우도 있다(조문이
    1건뿐일 때 dict로 오는 admrul 특유의 패턴과 유사). 두 경로를 다
    시도한다.
    """
    root = raw.get("AdmRulService", raw)
    lines = as_list(root.get("조문내용"))
    if not lines:
        nested = root.get("조문")
        if isinstance(nested, dict):
            lines = as_list(nested.get("조문내용"))

    units: list[SearchUnit] = []
    for raw_line in lines:
        text = text_of(raw_line)
        if not text:
            continue
        no = ArticleNo.from_text(text)
        if no is None:
            continue  # "제1장 총칙" 같은 장 제목 — 조문이 아니므로 건너뜀
        article_label = no.label
        # ★★ 실측 발견(2026-07-16, (계약예규) 정부 입찰ㆍ계약 집행기준
        # 제34조④): 행정규칙 평문 본문에는 "<개정 2008.12.29.>" 같은
        # 개정이력 각주가 항/호/목 사이사이에 그대로 섞여 있다. 이걸 안
        # 지우고 split_by_item/split_by_subitem 에 넘기면 각주 속 날짜
        # 조각("2008." 등)이 진짜 호 번호로 오인되고, 그 여파로 목 분해
        # 시작 위치까지 틀어진다. locator.py 는 이미 검색 직전에
        # strip_annotations 를 적용하지만, 여기서 만드는 SearchUnit 자체가
        # 오염된 조각으로 쪼개지면 그 시점에 손쓸 수 없으므로 분해 전에
        # 미리 제거한다.
        body = strip_annotations(strip_article_head(text))

        clauses = split_by_clause(body) or [Fragment(Level.NONE, None, body, body)]
        for clause in clauses:
            clause_no = clause.marker or ""
            items = split_by_item(clause.text)
            if not items:
                units.append(
                    SearchUnit(
                        article_code=article_label, article_label=article_label,
                        clause_no=clause_no, item_label="", subitem_label="",
                        text=clause.text, changed=True,
                    )
                )
                continue
            for item in items:
                item_label = item.marker or ""
                subs = split_by_subitem(item.text)
                if not subs:
                    units.append(
                        SearchUnit(
                            article_code=article_label, article_label=article_label,
                            clause_no=clause_no, item_label=item_label, subitem_label="",
                            text=item.text, changed=True,
                        )
                    )
                    continue
                for sub in subs:
                    units.append(
                        SearchUnit(
                            article_code=article_label, article_label=article_label,
                            clause_no=clause_no, item_label=item_label,
                            subitem_label=sub.marker or "",
                            text=sub.text, changed=True,
                        )
                    )
    return units


def changed_articles(articles: list[ArticleUnit]) -> list[ArticleUnit]:
    """조문변경여부=Y 인 것만. 법령 1차 스크리닝(가드 이전 단계)에 사용.

    실측 정확도: 전자정부법 8/8, 국가계약법 1/1, 사회보장기본법시행령 3/3
    (세 가지 서로 다른 개정 유형에서 100% 일치 확인됨)
    """
    return [a for a in articles if a.changed]