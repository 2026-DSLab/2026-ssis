"""신구법 <P> 태그 추출 (api/oldnew.py 의 old_texts/new_texts 를 소비).

api 레이어가 넘겨준 raw 텍스트(예: '…「통계법」제27조에 따라 <P>통계청이</P>
공표하는…')에서, 실제로 바뀐 부분만 뽑아내고 변경 유형을 분류한다.

이 모듈이 하지 않는 것:
    - 조/항/호/목 "위치"를 확정하는 것 (그건 locate/locator.py 의 책임 —
      이 모듈은 위치 확정에 쓸 '깨끗한 텍스트'만 준비한다)
    - lawService 전문과의 대조 (역시 locate 의 책임)

실측된 패턴 (전부 검증됨):
    1) 단순 치환    : <P>통계청이</P> → <P>국가데이터처가</P>
    2) 신설         : 구 쪽이 <P><신 설></P>, 신 쪽이 실제 내용 전체
    3) 삭제         : 특정 호가 "<P>1. 삭제</P>" 형태로 표시
    4) 안 바뀜      : "(생 략)" / "(현행과 같음)" — 검색/보고 대상 아님
    5) 대량 치환    : 한 조각 안에 <P> 가 여러 번 (국방데이터·인공지능업무
                      훈령: 위원 명단 조각 하나에 <P> 2회)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from lawtrack.text.split import CIRCLED, ArticleNo, parse_skip_range, strip_article_head

log = logging.getLogger(__name__)

_P_RE = re.compile(r"<P>(.*?)</P>", re.S)

_SKIP_MARKERS = ("(생 략)", "(생략)", "(현행과 같음)", "(현행과같음)")

#: <P> 안쪽 내용이 이 패턴이면 "이 항목이 삭제됐다"는 표시 (예: "4. 삭제").
_DELETED_RE = re.compile(r"^\s*\S*\s*삭\s*제\s*$")


class ChangeType(str, Enum):
    AMENDED = "개정"
    NEWLY_CREATED = "신설"
    DELETED = "삭제"
    UNCHANGED = "변경없음"
    UNKNOWN = "미상"


def strip_p_tags(text: str) -> str:
    """<P>…</P> 를 벗기고 안쪽 텍스트만 남긴 '깨끗한' 문장.

    locate 단계에서 lawService 전문과 대조할 때 쓴다. 전문 쪽에는애초에
    <P> 태그가 없으므로, 태그가 남아있으면 절대 매칭되지 않는다.
    """
    return _P_RE.sub(lambda m: m.group(1), text or "")


def extract_p_fragments(text: str) -> list[str]:
    """<P>…</P> 안쪽 내용만 순서대로 추출."""
    return [m.group(1) for m in _P_RE.finditer(text or "")]


def is_skippable(text: str) -> bool:
    """(생 략) / (현행과 같음) — 안 바뀐 조각인지."""
    compact = (text or "").replace(" ", "")
    return any(m.replace(" ", "") in compact for m in _SKIP_MARKERS)


#: "후단 신설"/"단서 신설"/"전단 신설" 같은 조각은 "이 조각의 일부만 새로
#: 생겼다"는 뜻이지 "조각 전체가 새로 생겼다"는 뜻이 아니다 — 이 세 접두어가
#: 붙은 마커는 "전체 신설/삭제" 판정에서 제외한다.
_PARTIAL_QUALIFIERS = ("후단", "단서", "전단")

#: ★ 실측 발견(2026-07-16, 별정우체국법): "<신  설>"처럼 공백이 2칸인 경우도
#: 있어 공백을 전부 제거하고 부분일치로 검사한다.
_NEWLY_CREATED_MARKERS = ("<신설>", "신설")
_DELETED_MARKERS = ("<삭제>", "삭제")


def _is_newly_created_marker(fragment: str) -> bool:
    """★★ 실측 발견(2026-07-16, 산업재해보상보험법 제116조② 등): "<후단
    신설>"/"<단서 신설>"처럼 "이 조각 중 일부만 새로 생겼다"는 뜻의 마커도
    예전엔 "신설"이라는 부분 문자열만 보고 무조건 "조각 전체가 신설"로
    분류했다. 실측 원문을 보면 old_text 쪽에 "② 사업주는…하여야 <P>한다
    </P>. <P><후단 신설></P>" 처럼 실제로 존재하던 내용(②…한다)이 그대로
    있는데도, change_type 이 "신설"로 찍혀 old_text가 있는데도 "신설"이라고
    LLM팀에 잘못 전달됐다.

    ★★ 실측 발견(2026-07-16, 표준 개인정보 보호지침): 처음엔 "괄호를 벗기면
    정확히 '신설'/'삭제' 두 글자만 남아야 인정"으로 너무 엄격하게 고쳤는데,
    이러면 "1. ∼5. 삭제"(1호부터 5호까지 전부 삭제되는 범위 표기) 같은
    정상적인 "조각 전체 삭제" 케이스까지 놓쳐서 회귀가 생겼다(실측: 재검증
    스윕에서 73464의 미확정 건수가 늘어나 발견). "1.∼5.삭제"는 부분삽입이
    아니라 그 조각 전체(1~5호)가 전부 삭제라는 뜻이므로 인정되어야 한다.
    진짜 구분 기준은 길이가 아니라 "후단/단서/전단" 접두어 유무다."""
    compact = (fragment or "").replace(" ", "")
    if any(q in compact for q in _PARTIAL_QUALIFIERS):
        return False
    return any(m in compact for m in _NEWLY_CREATED_MARKERS) and len(compact) <= 10


def _is_deleted_marker(fragment: str) -> bool:
    """★ _is_newly_created_marker 와 동일한 이유로, "후단삭제"/"단서삭제"
    처럼 일부만 삭제된 마커는 "조각 전체 삭제"로 오분류하면 안 된다."""
    if _DELETED_RE.match(fragment or ""):
        return True
    compact = (fragment or "").replace(" ", "")
    if any(q in compact for q in _PARTIAL_QUALIFIERS):
        return False
    return any(m in compact for m in _DELETED_MARKERS) and len(compact) <= 10


#: 조/항 전체 삭제를 나타내는 선행 항 기호(①-⑳).
_LEADING_CLAUSE_RE = re.compile(r"^[①-⑳]\s*")
#: 삭제일자를 나타내는 후행 괄호("(2004.11.12.)").
_TRAILING_DATE_PAREN_RE = re.compile(r"\(\s*[\d.]+\s*\)\s*$")


def _is_whole_block_deleted(new_text: str) -> bool:
    """★★ 실측 발견(2026-07-16, 조달청 내자구매업무 처리규정): "③ <삭제>
    (2004.11.12.)"처럼 <P> 태그로 아예 감싸이지 않은 채(new_fragments가
    빈 튜플) 항 기호+삭제마커+삭제일자가 통째로 한 덩어리로 오는 경우가
    있다. _is_deleted_marker는 <P> 안쪽 조각에만 적용되므로 이 형태는
    놓친다 — classify()가 AMENDED로 잘못 분류해, 존재할 수 없는 "③
    <삭제> (2004.11.12.)" 텍스트를 새 전문에서 찾으려다 항상 실패했다.
    선행 항 기호와 후행 삭제일자 괄호를 떼어내고 남는 게 삭제 마커뿐인지
    확인한다.
    """
    t = _LEADING_CLAUSE_RE.sub("", (new_text or "").strip())
    t = _TRAILING_DATE_PAREN_RE.sub("", t).strip()
    return _is_deleted_marker(t)


@dataclass(frozen=True)
class ArticleChange:
    """조각 하나(구/신 한 쌍)에 대한 변경 분석 결과."""

    index: int
    change_type: ChangeType

    old_raw: str    # <P> 포함 원문 (구)
    new_raw: str    # <P> 포함 원문 (신)
    old_clean: str  # <P> 제거 — locate 검색용 (구)
    new_clean: str  # <P> 제거 — locate 검색용 (신)

    old_fragments: tuple[str, ...] = field(default_factory=tuple)  # <P> 안쪽만 (구)
    new_fragments: tuple[str, ...] = field(default_factory=tuple)  # <P> 안쪽만 (신)

    @property
    def diff_pairs(self) -> list[tuple[str, str]]:
        """구/신 <P> 조각을 위치별로 짝지은 것.

        국방데이터·인공지능업무 훈령 사례처럼 한 조각에 <P> 가 여러 개면
        (위원 명단 나열) 등장 순서로 짝짓는다. 개수가 다르면 짧은 쪽
        기준으로 자르고 남은 쪽은 버리지 않고 단독 항목으로 남긴다.
        """
        n = min(len(self.old_fragments), len(self.new_fragments))
        pairs = list(zip(self.old_fragments[:n], self.new_fragments[:n]))
        if len(self.old_fragments) != len(self.new_fragments):
            log.debug(
                "index=%d <P> 개수 불일치: 구=%d 신=%d (짝 안 맞는 나머지는 diff_pairs에서 제외됨)",
                self.index, len(self.old_fragments), len(self.new_fragments),
            )
        return pairs


def classify(old_text: str, new_text: str) -> ChangeType:
    """구/신 한 쌍의 변경 유형 분류."""
    old_text = old_text or ""
    new_text = new_text or ""

    # 실측(국민체육진흥법 MST 286627): 신구조문 대비표가 이미 삭제되어 있던
    # 호를 구법 쪽의 "9. 삭  제"로만 남기고 신법 쪽은 빈 문자열로 주는
    # 경우가 있다. 이것은 이번 개정에서 삭제된 조문이 아니라 과거 삭제
    # 자리표시가 신법 표에서 생략된 것이므로 변경·미확정으로 보고하면 안 된다.
    if not strip_p_tags(new_text).strip() and _is_whole_block_deleted(strip_p_tags(old_text)):
        return ChangeType.UNCHANGED

    old_skip = is_skippable(old_text)
    new_skip = is_skippable(new_text)
    if old_skip and new_skip:
        return ChangeType.UNCHANGED

    old_frags = extract_p_fragments(old_text)
    new_frags = extract_p_fragments(new_text)

    if any(_is_newly_created_marker(f) for f in old_frags) and new_frags:
        return ChangeType.NEWLY_CREATED
    if any(_is_deleted_marker(f) for f in new_frags):
        return ChangeType.DELETED
    if not new_frags and _is_whole_block_deleted(strip_p_tags(new_text)):
        return ChangeType.DELETED
    if not old_frags and not new_frags:
        # <P> 표시가 아예 없는데 텍스트가 다르면 원인 불명 — 조용히 넘기지 않는다.
        if strip_p_tags(old_text) != strip_p_tags(new_text):
            return ChangeType.UNKNOWN
        return ChangeType.UNCHANGED
    return ChangeType.AMENDED


def build_change(index: int, old_text: str, new_text: str) -> ArticleChange:
    """조각 하나를 분석해 ArticleChange 로 만든다."""
    return ArticleChange(
        index=index,
        change_type=classify(old_text, new_text),
        old_raw=old_text or "",
        new_raw=new_text or "",
        old_clean=strip_p_tags(old_text or ""),
        new_clean=strip_p_tags(new_text or ""),
        old_fragments=tuple(extract_p_fragments(old_text or "")),
        new_fragments=tuple(extract_p_fragments(new_text or "")),
    )


def extract_changes(old_texts: list[str], new_texts: list[str]) -> list[ArticleChange]:
    """api/oldnew.py 의 old_texts/new_texts (같은 인덱스=같은 조문)를
    분석해 실제로 검토할 가치가 있는 변경 목록만 반환한다.

    UNCHANGED 는 결과에서 제외한다 — (생 략)/(현행과 같음) 은 애초에
    바뀐 게 아니므로 이후 파이프라인(locate, DB 적재)에서 다룰 필요가
    없다.
    """
    if len(old_texts) != len(new_texts):
        log.warning(
            "구조문/신조문 개수 불일치: 구=%d 신=%d — 짧은 쪽 기준으로 짝짓고 "
            "나머지는 버려짐 (원인 파악 필요할 수 있음)",
            len(old_texts), len(new_texts),
        )

    n = min(len(old_texts), len(new_texts))
    changes = []
    for i in range(n):
        change = build_change(i, old_texts[i], new_texts[i])
        if change.change_type is not ChangeType.UNCHANGED:
            changes.append(change)
    return changes


def extract_admrul_unchanged(
    old_texts: list[str], new_texts: list[str], touched_articles: set[str],
) -> dict[str, list[str]]:
    """행정규칙 전용: extract_changes()가 버리는 "(생략)/(현행과 같음)"
    스킵 표시 블록에서 실제 "안 바뀐" 항/호 라벨을 뽑는다.

    ★ 설계(2026-07-18): 법령은 항제개정유형이라는 공식 태그가 있어
    detect.py._unchanged_clauses()가 그걸 그대로 읽지만, admrul 본문
    (parse_admrul_units)은 평문이라 그런 태그가 없다. 대신 법제처 신구법
    비교 API 자체가 스킵 표시("1. ∼ 4. (생 략)")로 "이 범위는 안
    바뀌었다"를 이미 명시하고 있으므로(추론이 아니라 API가 준 사실), 그
    표시를 대신 근거로 쓴다.

    스킵 표시 블록은 자기 자신의 조문 헤더를 반복하지 않는 경우가
    대부분이라(같은 조문의 첫 블록만 "제N조(...)"로 시작), old_texts를
    원래 순서 그대로 훑으며 "지금 어느 조문 안인가"를 추적해야 한다
    (art_no.label이 바뀔 때만 current_article을 갱신).

    ★★ 실측 발견(2026-07-18, 공공기관의 데이터베이스 표준화 지침 제5조):
    호 번호는 항마다 새로 1부터 시작한다 — 제5조①에도 "1.~6."이 있고
    제5조③에도 별개로 "1.~6."이 있는데, 내용이 서로 다르다(①의 "1."은
    "다음 각목에 해당되는…"이고 ③의 "1."은 전혀 다른 항목). 처음 버전은
    조문 단위(current_article)만 추적해 두 클러스터의 스킵 라벨을
    "제5조": ["1.","3.","4.","5.","6."] 처럼 항 구분 없이 한 목록에
    섞어버렸다 — 그러면 "1."이 ①의 1.(실제로는 목 "라." 개정으로 바뀐
    항목)을 가리키는지 ③의 1.(진짜 안 바뀜)을 가리키는지 알 수 없어,
    LLM팀이 ①1.도 안 바뀐 것으로 잘못 읽을 위험이 있다. 그래서 항(①②③)
    마커도 함께 추적해(current_clause), 호 번호 라벨은 그 항 라벨을
    접두어로 붙여 "①3." 처럼 항상 자기 항이 명시된 형태로 낸다(이미
    SearchUnit.location_label 이 조/항/호/목을 이어붙이는 것과 같은
    표기 관례). 항 자체가 스킵된 경우(예: "④ (현행과 같음)")는 그 라벨
    자체가 이미 항 단위라 접두어가 필요 없다.

    ★ 실측 발견(같은 조문, 제5조①1.): "가. ∼ 다. (생 략)" 처럼 목(目)
    단위 스킵도 실제로 존재한다(이전 설계 메모의 "아직 미관측" 가정은
    틀렸음 — 정정). 다만 이 함수는 아직 목 단위 확장은 구현하지 않는다
    (parse_skip_range가 circled/item 패턴만 인식) — 목 단위 마커는
    그냥 무시되어 조용히 스킵된다(틀린 답을 내는 대신 아무 답도 안
    내는 쪽이 안전). 나중에 필요해지면 같은 매커니즘(SUBITEM 순서
    lookup)으로 확장 가능.

    touched_articles: 이번 처리에서 이미 CHANGED로 감지된 조문만 결과에
    포함한다 — law쪽 _unchanged_clauses()의 touched_articles 필터와 동일한
    원칙(관심 밖 조문에 대해 "안 바뀜"을 굳이 보고하지 않는다).
    """
    out: dict[str, list[str]] = {}
    current_article: str | None = None
    current_clause: str | None = None
    n = min(len(old_texts), len(new_texts))
    for i in range(n):
        text = strip_p_tags(old_texts[i] or "").strip()
        art_no = ArticleNo.from_text(text)
        if art_no is not None:
            current_article = art_no.label
            current_clause = None
            # ★ 실측(2026-07-18, 42496 제12조①): 스킵 표시가 조문 헤더와
            # 한 블록에 같이 오는 경우가 있다("제12조(위탁용역 보정대가)
            # ① (생 략)") — 헤더를 뗀 나머지에서 스킵 마커를 찾아야 한다.
            text = strip_article_head(text)

        if text and text[0] in CIRCLED:
            current_clause = text[0]

        labels = parse_skip_range(text)
        if labels is None or current_article is None:
            continue
        if current_article not in touched_articles:
            continue
        bucket = out.setdefault(current_article, [])
        for lbl in labels:
            # 호(1. 2. …) 라벨만 항 접두어가 필요 — 항(①②…) 라벨은 그
            # 자체로 이미 어느 항인지 명시돼 있다.
            out_label = f"{current_clause}{lbl}" if lbl.endswith(".") and current_clause else lbl
            if out_label not in bucket:
                bucket.append(out_label)
    return out
