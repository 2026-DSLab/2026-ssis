"""watchlist 매칭 엔진.

방식:
1. 페이지 텍스트를 norm() 과 동일 규칙으로 문자 단위 압축하되,
   압축 문자열 각 위치 → 원문 위치 매핑을 유지한다 (스니펫 복원용).
2. 사전 키를 긴 것부터 검색하고, 이미 더 긴 매칭이 덮은 구간은
   건너뛴다 ("개인정보보호법" ⊂ "개인정보보호법시행령" 이중 계상 방지).
3. 낫표·따옴표 인용 중 사전 매칭에 실패했고 법령류 접미로 끝나는
   텍스트를 '감시 대상 외 후보'로 수집한다.
"""
import re
import unicodedata
from dataclasses import dataclass

from doc_match.dictionary import Dictionary
from doc_match.normalize import _MIDDLE_DOTS, _PAREN_PREFIX, norm

_QUOTED = re.compile(
    "[「『][^「」『』]{2,60}?[」』]"
    "|[\u2018'][^\u2018\u2019']{2,60}?[\u2019']"
)
_LAW_SUFFIX = re.compile(
    r"(법|법률|시행령|시행규칙|지침|고시|기준|조건|규정|요령|세칙|예규)$"
)
_STRIP_CHARS = set("「」『』\"\u201c\u201d'\u2018\u2019") | set(" \t\n\r\x0b\x0c")


@dataclass
class Hit:
    law_id: str
    page: int  # 1-based
    snippet: str


@dataclass
class Candidate:  # 감시 대상 외 후보
    raw: str
    page: int


@dataclass
class MatchResult:
    hits: list[Hit]
    candidates: list[Candidate]


def _compress(page_text: str) -> tuple[str, list[int]]:
    """norm 과 동일 규칙으로 압축 + 원문 위치 매핑.

    괄호 접두 제거는 이름 단위 규칙이라 전문 압축에는 적용하지 않는다
    (사전 키 쪽에서 이미 제거되므로 본문의 "(계약예규) X"는
    접두 뒤 X 부분에서 매칭된다).
    """
    chars, idx = [], []
    for i, ch in enumerate(page_text):
        if ch in _STRIP_CHARS or _MIDDLE_DOTS.fullmatch(ch):
            continue
        chars.append(ch)
        idx.append(i)
    return "".join(chars), idx


def match_pages(pages: list[str], d: Dictionary, snippet_width: int = 40) -> MatchResult:
    hits: list[Hit] = []
    candidates: list[Candidate] = []
    keys = d.keys_longest_first

    for page_no, raw in enumerate(pages, start=1):
        flat, idx = _compress(raw)
        covered = bytearray(len(flat))
        for k in keys:
            start = 0
            while True:
                i = flat.find(k, start)
                if i < 0:
                    break
                end = i + len(k)
                if not any(covered[i:end]):
                    covered[i:end] = b"\x01" * len(k)
                    a = max(0, idx[i] - snippet_width)
                    b = min(len(raw), idx[end - 1] + snippet_width)
                    snippet = re.sub(r"\s+", " ", raw[a:b]).strip()
                    hits.append(Hit(d.alias[k], page_no, snippet))
                start = i + 1

        # 감시 대상 외 후보: 인용부호 안 + 법령류 접미 + 사전 매칭 실패
        for m in _QUOTED.finditer(raw):
            inner = m.group(0)[1:-1].strip()
            key = norm(inner)
            if not key or key in d.alias:
                continue
            if _LAW_SUFFIX.search(key):
                candidates.append(
                    Candidate(_PAREN_PREFIX.sub("", inner), page_no)
                )
    return MatchResult(hits, candidates)
