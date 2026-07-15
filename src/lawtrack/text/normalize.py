"""문자열 정규화.

이 모듈 하나가 실측으로 확인된 매칭 실패 20건 이상을 해소한다.

핵심 사실 (실측 검증됨):
  - 가운뎃점처럼 보이는 문자가 5종이며, 서로 다른 코드포인트다.
  - unicodedata.normalize('NFC') 로도, 더 강한 'NFKC' 로도 통일되지 않는다.
  - MySQL utf8mb4_unicode_ci(UCA) 콜레이션으로도 동일 취급되지 않는다.
  => 커스텀 매핑 테이블이 유일한 해법이며, 반드시 애플리케이션 레이어에서
     INSERT/조회 전에 적용해야 한다.

정규화 방향은 ㆍ(U+318D)로 통일한다. 국가법령정보 API 응답이 이 문자를
사용하는 것이 실측으로 확인되었기 때문이다.
  예) '10ㆍ29이태원참사', '국방데이터ㆍ인공지능', '초ㆍ중등교육법'
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# 문자 매핑
# ---------------------------------------------------------------------------

#: 가운뎃점 5종 → ㆍ(U+318D)
#:
#: U+2024(ONE DOT LEADER)는 팀 정리 문서에는 없었으나 전자정부법 조문
#: "관리적․기술적" 에서 실제로 발견되어 추가했다. 빠뜨리면 해당 조문만
#: 조용히 매칭 실패한다.
DOT_MAP: dict[str, str] = {
    "\u00B7": "\u318D",  # · MIDDLE DOT
    "\u2027": "\u318D",  # ‧ HYPHENATION POINT
    "\u30FB": "\u318D",  # ・ KATAKANA MIDDLE DOT
    "\u2024": "\u318D",  # ․ ONE DOT LEADER   ← 실측 추가분
    "\u2219": "\u318D",  # ∙ BULLET OPERATOR
    "\u22C5": "\u318D",  # ⋅ DOT OPERATOR
}

#: 물결표 3종 → ~(U+007E)
#: 실제 조문 "③ ∼ ⑨ (생 략)" 에서 U+223C 사용이 확인되었다.
TILDE_MAP: dict[str, str] = {
    "\u223C": "~",  # ∼ TILDE OPERATOR
    "\uFF5E": "~",  # ～ FULLWIDTH TILDE
    "\u301C": "~",  # 〜 WAVE DASH
}

#: 낫표/따옴표류. 문서마다 제각각이라 통일한다.
QUOTE_MAP: dict[str, str] = {
    "\u2018": "'",  # ‘
    "\u2019": "'",  # ’
    "\u201C": '"',  # “
    "\u201D": '"',  # ”
}

_CHAR_MAP = {**DOT_MAP, **TILDE_MAP, **QUOTE_MAP}
_CHAR_TABLE = str.maketrans(_CHAR_MAP)

#: 행정규칙 접두사. "(계약예규) 예정가격 작성기준" vs "예정가격작성기준"
_PREFIX_RE = re.compile(r"^\s*\((?:계약예규|회계예규|조달청)\)\s*")

#: "~에 관한 법" ↔ "~에 관한 법률" 접미사 생략형
#: (공백 제거 후 적용하는 것을 전제로 한다)
_SUFFIX_LAW_RE = re.compile(r"에관한법$")

_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# 기본 정규화
# ---------------------------------------------------------------------------

def normalize_chars(s: str) -> str:
    """유니코드 정규화 + 커스텀 문자 매핑.

    NFC 는 조합형/완성형 한글 통일에는 필요하므로 먼저 적용하되,
    가운뎃점 5종은 NFC/NFKC 로 해결되지 않으므로 별도 매핑한다.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    return s.translate(_CHAR_TABLE)


def strip_spaces(s: str) -> str:
    """공백 완전 제거.

    실측: 띄어쓰기 '한 칸' 차이로 매칭 실패가 발생한다.
      - 조달청 협상에 의한 계약 제안서평가 세부기준 (<P> 9개 중 일부)
      - 조달청 내자구매업무 처리규정
      - 개인정보의 기술적ㆍ관리적 보호조치 기준
    부분 공백 정규화(연속공백→1칸)로는 부족하므로 전부 제거 후 비교한다.
    """
    return _WS_RE.sub("", s or "")


def collapse_spaces(s: str) -> str:
    """연속 공백 → 1칸. 화면 출력/로그용."""
    return _WS_RE.sub(" ", (s or "")).strip()


# ---------------------------------------------------------------------------
# 용도별 정규화
# ---------------------------------------------------------------------------

def normalize_name(s: str) -> str:
    """법령명/행정규칙명 매칭용 정규화 키.

    적용 항목:
      1) 유니코드 문자 매핑 (점 5종, 물결표, 따옴표)
      2) 접두사 제거        - "(계약예규) 예정가격 작성기준"
      3) 공백 전부 제거     - "노인일자리" vs "노인 일자리"
      4) 접미사 보정        - "~에 관한 법" → "~에 관한 법률"

    주의: 약칭(사회보장급여법 → 사회보장급여의 이용ㆍ제공 및 …)은
    정규화로 해결되지 않는다. API 응답의 `법령약칭명` 필드로 교차검증하거나
    수동 매핑 테이블(seed)로 처리해야 한다.
    """
    s = normalize_chars(s)
    s = _PREFIX_RE.sub("", s)
    s = strip_spaces(s)
    s = _SUFFIX_LAW_RE.sub("에관한법률", s)
    return s


def normalize_text(s: str) -> str:
    """조문 본문 검색용 정규화 키.

    normalize_name 과 달리 접미사/접두사 변환을 하지 않는다.
    조문 내용 자체를 건드리면 의미가 바뀔 수 있기 때문이다.
    """
    return strip_spaces(normalize_chars(s))


def names_match(a: str, b: str) -> bool:
    """법령명 동일 여부 (정규화 후 완전일치).

    실측: `query` 는 부분일치라 무관한 법이 대량 혼입된다.
      - "국가를 당사자로 하는 계약에 관한 법률" → totalCnt=10, 무관 7건
        (지방자치단체를~, 특정물품등의조달에관한~, 특정조달을 위한~)
      - "보조금 관리에 관한 법" → 지방자치단체 보조금 관리에 관한 법률 혼입
    따라서 부분일치(in)가 아니라 완전일치로 판정한다.
    """
    return normalize_name(a) == normalize_name(b)


def text_contains(haystack: str, needle: str) -> bool:
    """정규화 후 포함 여부."""
    if not needle:
        return False
    return normalize_text(needle) in normalize_text(haystack)


def count_occurrences(haystack: str, needle: str) -> int:
    """정규화 후 등장 횟수. 중복 매칭 판정에 사용."""
    if not needle:
        return 0
    return normalize_text(haystack).count(normalize_text(needle))