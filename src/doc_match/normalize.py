"""법령명 정규화.

문서·watchlist 양쪽 명칭을 동일한 canonical key로 변환해 비교함.
처리 대상 변형은 4개 실문서(표준가이드 요약본, 대가산정 가이드,
제안요청서 템플릿, PMO 매뉴얼) 전수 스캔에서 실측된 것들임:
- 가운뎃점 4종 혼용: · ㆍ ‧ ․ (+ ∙ •)
- 띄어쓰기 유무: "개인정보 보호법" / "개인정보보호법"
- 괄호 접두: "(계약예규) 용역계약일반조건", "(사규) ...", "(행정안전부) ..."
- 인용부호 혼용: 「」 『』 '' "" ‘’ “”
"""
import re
import unicodedata

_PAREN_PREFIX = re.compile(
    r"^\((계약예규|계약일반|사규|행정안전부|개인정보보호위원회|보건복지부)\)\s*"
)
_MIDDLE_DOTS = re.compile(r"[·ㆍ‧․∙•]")
_QUOTES_SPACES = re.compile("[「」『』\"\u201c\u201d'\u2018\u2019\\s]")


def norm(s: str) -> str:
    """canonical key 생성. 빈 문자열이 나올 수 있으므로 호출측에서 확인할 것."""
    s = unicodedata.normalize("NFC", s)
    s = _PAREN_PREFIX.sub("", s)
    s = _MIDDLE_DOTS.sub("", s)
    s = _QUOTES_SPACES.sub("", s)
    return s
