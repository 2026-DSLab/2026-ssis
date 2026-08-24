"""위치 라벨(location_label) 문자열 처리 — 조문/위치 분리 + 항/호/목을
사람이 읽는 표기로 바꾸기.

왜 여기 하나로 모으는가: 이 규칙은 원래 report/builder.py
(HWPX 표의 "조문"/"위치" 칸)에만 있었는데, agents.py의 "번호만 이동"
문장("②5.에서 제8조②12.로 이동했습니다")도 LLM을 부르지 않는 규칙 기반
문장이라 똑같은 위치 표기 규칙이 그대로 필요해졌음
("②5." 대신 "2항 5호"처럼 읽히게 해 달라는 요구). 완전히 같은 로직을
agents.py에 또 복사하는 대신, summarizer 패키지 안에서 공유함 — 두
호출부 다 무거운 의존성(hwpx 등)이 필요 없는 순수 문자열 함수라 공유해도
비용이 없음. (webapp/app.py는 별도로 자기 사본을 유지함 — 그쪽은
HTML span으로 감싸야 해서 반환 타입 자체가 다르고, 무거운 hwpx 패키지를
끌어오면 안 되는 별도 이유가 있음: report/builder.py 상단 주석 참고.)
"""

from __future__ import annotations

import re

_ARTICLE = re.compile(r"^제\d+조(?:의\d+)?")
_POS_TOKEN_RE = re.compile(r"[①-⑳]|[가-힣](?:의\d+)?\.|\d+(?:의\d+)?\.|\d+\)")


def article_of(location_label: str) -> str:
    """location_label 에서 조문 부분만("제9조", "제9조의2"). 조 번호가
    없으면(항/호만 있는 값) 전체를 그대로 돌려줌."""
    m = _ARTICLE.match(location_label or "")
    return m.group(0) if m else (location_label or "")


def position_of(location_label: str) -> str:
    """location_label 에서 조문을 뗀 나머지(항/호/목 부분)."""
    label = location_label or ""
    art = article_of(label)
    return label[len(art):] if label.startswith(art) else ""


def format_position(rest: str) -> str:
    """위치 문자열(예: '⑥1.가.')을 사람이 읽는 '⑥항·1호·가목'으로 바꿈.

    숫자 뒤에 "항"/"호"/"목" 글자를 붙이고, 조각 사이는 가운뎃점(·)으로
    구분함(원래 표기의 마침표는 한글 단위로 대체되므로 뗌).
    """
    if not rest:
        return rest
    out: list[str] = []
    pos = 0
    for tm in _POS_TOKEN_RE.finditer(rest):
        if tm.start() > pos:
            out.append(rest[pos:tm.start()])
        elif out:
            out.append("·")
        tok = tm.group(0)
        if re.fullmatch(r"[①-⑳]", tok):
            suffix, num = "항", tok
        elif re.fullmatch(r"[가-힣](?:의\d+)?\.", tok):
            suffix, num = "목", tok[:-1]
        else:
            suffix, num = "호", tok.rstrip(".)")
        out.append(f"{num}{suffix}")
        pos = tm.end()
    if pos < len(rest):
        out.append(rest[pos:])
    return "".join(out)


def format_location(location_label: str) -> str:
    """location_label 전체(조문+위치, 또는 위치만)를 사람이 읽는 표기로.

    "제9조⑥1." → "제9조 ⑥항·1호"
    "⑥1."(조 번호 없이 위치만 — 같은 조 안에서 옮겨진 이동 전 위치 등) → "⑥항·1호"
    """
    label = location_label or ""
    if not label:
        return label
    art = article_of(label)
    if art and label.startswith(art) and art != label:
        rest = format_position(label[len(art):])
        return f"{art} {rest}" if rest else art
    return format_position(label)
