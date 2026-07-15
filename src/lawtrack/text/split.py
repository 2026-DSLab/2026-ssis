"""조문 문장 분해 (가드 ④).

문제 (실측):
    oldAndNew  : "① 텍스트 ② 텍스트 ③ 텍스트"   ← 한 덩어리 문자열
    lawService : <항번호>①</항번호><항내용>텍스트</항내용>
                 <항번호>②</항번호><항내용>텍스트</항내용>   ← 태그로 분리

    => oldAndNew 에서 통짜로 가져온 문장을 그대로 lawService 전문에서
       검색하면 절대 안 나온다.

확인된 발생 법령:
    - 전자정부법   : 한 번에 입력 시 실패. ①② 로 나눠 검색해야 함
    - 도로교통법   : oldAndNew 한 줄 vs lawService 태그 분리
    - 별정우체국법 : 항ㆍ호ㆍ목 기호 기준 정규식 분해 필요
    - 실종아동법   : <P> 문장 전체로 검색 시 실패
                     (조문 내용이 한 문장에 붙어있는 경우 발생)

해결:
    항(①②③) / 호(1. 2.) / 목(가. 나.) 기호를 경계로 문장을 조각내고,
    조각별로 검색한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Level(str, Enum):
    """조문 계층."""

    CLAUSE = "항"      # ① ② ③
    ITEM = "호"        # 1. 2. 3.  /  1의2.
    SUBITEM = "목"     # 가. 나. 다.
    NONE = "없음"      # 기호 없는 평문


# ---------------------------------------------------------------------------
# 기호 정의
# ---------------------------------------------------------------------------

#: 항 기호 ①(U+2460) ~ ⑳(U+2473). 법령은 보통 ⑮ 이내이나 여유를 둔다.
CIRCLED = "".join(chr(c) for c in range(0x2460, 0x2474))

#: 호: "1." "12." "1의2." — 줄 시작 또는 공백 뒤에서만 인식한다.
#: 주의: "제27조" 의 "27" 같은 조문 참조를 호로 오인하면 안 되므로
#:       뒤에 반드시 마침표가 오는 경우만 잡는다.
_ITEM_PAT = r"\d+(?:의\d+)?\."

#: 목: "가." "나." … "카." (한글 자모 순)
_SUBITEM_CHARS = "가나다라마바사아자차카타파하"
_SUBITEM_PAT = rf"[{_SUBITEM_CHARS}](?:의\d+)?\."

_CLAUSE_RE = re.compile(rf"(?=[{CIRCLED}])")
#: 실측 발견: oldAndNew 블록에서 문장이 "...하여야 한다.1. 정보시스템..." 처럼
#: 마침표 바로 뒤에 공백 없이 호 번호가 붙는 경우가 있다(전자정부법 제56조의2
#: 실측 확인). 반면 아래 두 경우는 호 경계로 오인하면 안 된다:
#:   - "2.1% 이상" 같은 소수점 표기 (직전 2글자가 "숫자+마침표")
#:   - "12의2." 같은 가지번호 표기의 뒷부분 (직전 2글자가 "숫자+의")
#: 한국어 문장 종결 "…다." 뒤에 오는 숫자는 이 두 조건에 안 걸리므로
#: (마침표 앞이 한글이라 "숫자+마침표"도 "숫자+의"도 아님) 정상 분해된다.
_ITEM_RE = re.compile(
    rf"(?:(?<=^)|(?:(?<!\d)(?<!\d\.)(?<!\d의)))(?={_ITEM_PAT})"
)
_SUBITEM_RE = re.compile(
    rf"(?:(?<=^)|(?:(?<!\d)(?<!\d\.)(?<!\d의)))(?={_SUBITEM_PAT})"
)

_CLAUSE_HEAD_RE = re.compile(rf"^([{CIRCLED}])")
_ITEM_HEAD_RE = re.compile(rf"^({_ITEM_PAT})")
_SUBITEM_HEAD_RE = re.compile(rf"^({_SUBITEM_PAT})")

#: 조문 제목 "제6조의2(기준 중위소득의 산정)" 형태
_ARTICLE_HEAD_RE = re.compile(r"^제(\d+)조(?:의(\d+))?\s*(?:\(([^)]*)\))?")

#: 변경 없음 표시. 이 조각들은 검색 대상이 아니다.
_SKIP_MARKERS = ("(생 략)", "(생략)", "(현행과 같음)", "(현행과같음)")


@dataclass(frozen=True)
class Fragment:
    """분해된 조각 하나."""

    level: Level
    marker: str | None   # "①", "12의2.", "가." / 없으면 None
    text: str            # 기호를 제외한 본문
    raw: str             # 기호 포함 원문

    @property
    def is_skippable(self) -> bool:
        """(생 략) / (현행과 같음) 처럼 내용이 없는 조각."""
        compact = self.text.replace(" ", "")
        return any(m.replace(" ", "") in compact for m in _SKIP_MARKERS)

    @property
    def searchable(self) -> bool:
        """검색 대상으로 쓸 만한 조각인가."""
        return bool(self.text.strip()) and not self.is_skippable


# ---------------------------------------------------------------------------
# 분해
# ---------------------------------------------------------------------------

def split_by_clause(text: str) -> list[Fragment]:
    """항 기호(①②③) 기준 분해."""
    return _split(text, _CLAUSE_RE, _CLAUSE_HEAD_RE, Level.CLAUSE)


def split_by_item(text: str) -> list[Fragment]:
    """호 기호(1. 2.) 기준 분해."""
    return _split(text, _ITEM_RE, _ITEM_HEAD_RE, Level.ITEM)


def split_by_subitem(text: str) -> list[Fragment]:
    """목 기호(가. 나.) 기준 분해."""
    return _split(text, _SUBITEM_RE, _SUBITEM_HEAD_RE, Level.SUBITEM)


def _split(text: str, split_re, head_re, level: Level) -> list[Fragment]:
    if not text or not text.strip():
        return []

    parts = [p for p in split_re.split(text) if p.strip()]
    out: list[Fragment] = []
    for part in parts:
        stripped = part.strip()
        m = head_re.match(stripped)
        if m:
            marker = m.group(1)
            body = stripped[m.end():].strip()
            out.append(Fragment(level, marker, body, stripped))
        else:
            out.append(Fragment(Level.NONE, None, stripped, stripped))
    return out


def split_all(text: str) -> list[Fragment]:
    """항 → 호 → 목 순으로 계층 분해하여 최소 단위 조각들을 반환.

    검색 시에는 가장 작은 단위부터 시도하는 것이 매칭 확률이 높다.
    단, 조각이 너무 짧아지면 중복 매칭이 늘어나므로 가드 ⑤(번호 결합)로
    보완해야 한다.
    """
    if not text or not text.strip():
        return []

    result: list[Fragment] = []
    for clause in split_by_clause(text) or [Fragment(Level.NONE, None, text, text)]:
        items = split_by_item(clause.text)
        if len(items) <= 1:
            result.append(clause)
            continue
        for item in items:
            subs = split_by_subitem(item.text)
            if len(subs) <= 1:
                result.append(item)
            else:
                result.extend(subs)
    return result


def searchable_fragments(text: str) -> list[Fragment]:
    """검색에 쓸 조각만. (생 략) / (현행과 같음) 제외."""
    return [f for f in split_all(text) if f.searchable]


# ---------------------------------------------------------------------------
# 조문 번호
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleNo:
    """조문 번호.

    API 의 6자리 조문번호 인코딩:
        앞 4자리 = 조번호, 뒤 2자리 = 가지번호("의N", 없으면 00)
        예) '000602' → 제6조의2   /  '002100' → 제21조
    """

    number: int
    branch: int = 0

    @property
    def label(self) -> str:
        return f"제{self.number}조" + (f"의{self.branch}" if self.branch else "")

    @classmethod
    def from_code(cls, code: str) -> "ArticleNo":
        """6자리 코드 파싱. '000602' → 제6조의2"""
        code = (code or "").strip()
        if not code.isdigit() or len(code) != 6:
            raise ValueError(f"조문번호 코드가 6자리 숫자가 아님: {code!r}")
        return cls(number=int(code[:4]), branch=int(code[4:]))

    @classmethod
    def from_text(cls, text: str) -> "ArticleNo | None":
        """'제6조의2(기준 중위소득의 산정) …' 같은 문장에서 조번호 추출."""
        m = _ARTICLE_HEAD_RE.match((text or "").strip())
        if not m:
            return None
        return cls(number=int(m.group(1)), branch=int(m.group(2) or 0))

    def to_code(self) -> str:
        return f"{self.number:04d}{self.branch:02d}"


def article_title(text: str) -> str | None:
    """'제6조의2(기준 중위소득의 산정)' → '기준 중위소득의 산정'"""
    m = _ARTICLE_HEAD_RE.match((text or "").strip())
    return m.group(3) if m and m.group(3) else None


def strip_article_head(text: str) -> str:
    """맨 앞의 '제N조(의M)(제목)' 헤더를 제거하고 본문만 남긴다.

    실측 발견: oldAndNew 블록이 "제6조의2(기준 중위소득의 산정) ① 기준
    중위소득은 …" 처럼 조문 제목으로 시작하는 경우가 있다. 이 제목
    부분은 실제 조문 '내용'이 아니라 헤더이므로, lawService 항내용
    필드에는 보통 포함되지 않는다. 그대로 두면 항상 0건 매칭되는
    무의미한 조각이 하나 더 생기므로, 위치 검색 전에 제거한다.
    """
    text = (text or "").strip()
    m = _ARTICLE_HEAD_RE.match(text)
    if not m:
        return text
    return text[m.end():].strip()