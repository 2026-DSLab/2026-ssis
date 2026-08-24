"""어절 단위 텍스트 비교.

선별(triage)과 문서 렌더러가 함께 쓰는 토대임.

--------------------------------------------------------------------------
[왜 글자 단위가 아니라 어절 단위인가 — 실측]

    difflib 을 글자 단위로 돌리면 한국어에서 이런 결과가 나옴:

        '통계청'  ->  '국가데'
        ''       ->  '터처가'

    SequenceMatcher 가 '장이' 같은 공통 글자에 정렬을 맞추면서
    '통계청장이' -> '국가데이터처장이' 라는 하나의 치환이
    무의미한 조각 두 개로 쪼개짐.

    결과:
      - 선별: 변경 어절을 기관명 패턴과 대조할 수 없음('국가데'는 기관명이 아님)
      - 렌더러: 대비표 강조가 단어 중간에서 끊겨 읽기 어려움

    => 공백 기준 어절로 토큰화한 뒤 비교함.

출처: seongbeen2 브랜치(lawtrack.mas.textdiff)에서 이식.

--------------------------------------------------------------------------
[summarizer.matching.describe_change() 와의 관계]

    이 모듈(화면 강조·선별용)과 summarizer.matching.describe_change()
    (LLM 프롬프트 조각 생성용)는 둘 다 "구/신 텍스트를 어절 단위로
    비교한다"는 tokenize()를 공유한다(예전엔 matching.py 가 글자 단위를
    써서 "20일"→"30일"을 "2"→"3"으로 쪼개는 버그가 있었음 — 실측 사고
    이후 이쪽 토큰화로 통일했음).

    그 위의 조각 다듬기 로직은 일부러 통일하지 않았음 — 용도가 다름:
      - 이 모듈의 목록-항목 인식(_split_list_items/_diff_list_items)은
        "쉼표로 나열된 목록에서 재배열된 항목을 화면에 정확히 밑줄 긋기"
        위한 것임. LLM 프롬프트 조각에는 필요 없음 — LLM은 밑줄이
        아니라 문장으로 다시 쓰므로, 재배열이든 아니든 조각만 정확하면
        됨.
      - describe_change()의 근접-조각 병합(_merge_close_opcodes)은
        "한 단어가 문장 안에서 자리만 옮기면 그 앞뒤를 통째로 하나의
        치환으로 묶어야 LLM이 비문을 안 만든다"는 것임. 화면 강조에는
        필요 없음 — 밑줄은 정확한 위치에만 그으면 되고, 옮겨간 단어를
        하나로 묶어 보여줄 필요가 없음.
    두 로직을 억지로 하나로 합치면 한쪽 용도에는 과하고 다른 쪽엔
    부족해짐. 토큰화(무엇을 한 단위로 볼 것인가)만 공유하고, 그 위의
    정렬/병합 규칙은 소비자별로 따로 두는 것이 지금까지 실측으로 확인된
    두 요구사항에 맞음.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

__all__ = [
    "Segment",
    "tokenize",
    "diff_segments",
    "changed_words",
    "similarity",
    "change_ratio",
]

_TOKEN_RE = re.compile(r"\s+|[^\s]+")


def tokenize(text: str) -> list[str]:
    """공백을 보존한 채 어절로 나눔.

    공백을 버리면 재결합 시 원문이 복원되지 않아 대비표에 쓸 수 없음.
    """
    return _TOKEN_RE.findall(text or "")


@dataclass(frozen=True)
class Segment:
    """비교 결과 한 조각.

    kind: 'eq'(동일) | 'del'(현행에만) | 'ins'(개정안에만)
    """

    text: str
    kind: str


def diff_segments(old: str, new: str) -> tuple[list[Segment], list[Segment]]:
    """(현행 세그먼트, 개정안 세그먼트) 를 돌려줌.

    각 리스트의 text 를 순서대로 이으면 원문이 그대로 복원됨.
    """
    a, b = tokenize(old), tokenize(new)
    left, right = _diff_tokens(a, b)
    return _merge(left), _merge(right)


_LIST_SEP_RE = re.compile(r",\s*|\s+및\s+|\s+또는\s+")


def _diff_tokens(a: list[str], b: list[str]) -> tuple[list[Segment], list[Segment]]:
    """어절 리스트 두 개를 비교함.

    공통 접두부터 뗌 (공공기관의 정보공개에 관한 법률
    시행령 제20조② 실측): "…위원은 기획재정부 제2차관, 법무부 차관,
    행정안전부 차관 및…" → "…위원은 법무부 차관, 행정안전부 차관,
    기획예산처 차관 및…"에서, 접두("…위원은 ")를 안 떼고 목록 항목으로
    바로 쪼개면 그 접두가 신·구 각각의 첫 항목에 서로 다르게 눌러붙어
    ("…위원은 기획재정부 제2차관" vs "…위원은 법무부 차관") 첫 항목끼리
    영원히 매칭에 실패함 — 그래서 접두는 반드시 먼저 뗌.

    접미는 일부러 여기서 따로 떼지 않음 — 처음엔 접두와 대칭으로
    공통 접미도 토큰 단위로 미리 떼려 했는데, 그러면 "차관"이 신·구
    양쪽에 반복되는 탓에(구법엔 1번, 신법엔 2번) 접미 트리밍이 신법 쪽의
    "기획예산처" 바로 뒤 "차관"까지 통째로 삼켜버려, 정작 "행정안전부"
    항목에 붙어야 할 "차관"이 사라지고 그 결과가 오히려 더 나빠졌음
    (실측: "행정안전부"만 남고 " 차관, 기획예산처"가 전부 삽입으로
    잘못 강조됨). 접두를 뗀 나머지를 그대로 _diff_core(목록 항목 단위
    비교)에 넘기면, 마지막 공통 항목("국무조정실 국무1차장으로 한다.")은
    항목 비교 자체가 자연스럽게 '동일'로 잡아줌 — 별도 접미 트리밍이
    필요 없음.
    """
    n = min(len(a), len(b))
    p = 0
    while p < n and a[p] == b[p]:
        p += 1

    left: list[Segment] = []
    right: list[Segment] = []
    if p:
        left.append(Segment("".join(a[:p]), "eq"))
        right.append(Segment("".join(b[:p]), "eq"))

    mid_left, mid_right = _diff_core(a[p:], b[p:])
    left.extend(mid_left)
    right.extend(mid_right)

    return left, right


def _diff_core(a: list[str], b: list[str]) -> tuple[list[Segment], list[Segment]]:
    """접두/접미를 뗀 나머지. 목록처럼 보이면 항목 단위로, 아니면 어절
    단위(SequenceMatcher)로 비교함."""
    if not a or not b:
        left = [Segment("".join(a), "del")] if a else []
        right = [Segment("".join(b), "ins")] if b else []
        return left, right

    items_a = _split_list_items("".join(a))
    items_b = _split_list_items("".join(b))
    if len(items_a) > 1 and len(items_b) > 1:
        return _diff_list_items(items_a, items_b)

    return _diff_plain(a, b)


def _split_list_items(text: str) -> list[tuple[str, str]]:
    """쉼표/"및"/"또는"으로 나열된 목록을 (항목 내용, 뒤따르는 구분자)
    쌍의 리스트로 쪼갬. 각 쌍의 두 문자열을 이어붙이면 그 항목의 원문이
    그대로 복원되고, 전체를 이어붙이면 text 전체가 복원됨."""
    parts = _LIST_SEP_RE.split(text)
    seps = _LIST_SEP_RE.findall(text)
    items = []
    for i, content in enumerate(parts):
        sep = seps[i] if i < len(seps) else ""
        items.append((content, sep))
    return items


def _diff_list_items(
    items_a: list[tuple[str, str]], items_b: list[tuple[str, str]]
) -> tuple[list[Segment], list[Segment]]:
    """목록 항목 단위로 맞춰봄 — 구분자 차이는 비교에서 무시하고,
    안 맞는 항목끼리만 어절 단위로 재귀 비교함."""
    contents_a = [c.strip() for c, _ in items_a]
    contents_b = [c.strip() for c, _ in items_b]
    sm = SequenceMatcher(None, contents_a, contents_b, autojunk=False)

    left: list[Segment] = []
    right: list[Segment] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                c, sep = items_a[i1 + k]
                left.append(Segment(c + sep, "eq"))
                cb, sepb = items_b[j1 + k]
                right.append(Segment(cb + sepb, "eq"))
        elif tag == "delete":
            for c, sep in items_a[i1:i2]:
                left.append(Segment(c + sep, "del"))
        elif tag == "insert":
            for c, sep in items_b[j1:j2]:
                right.append(Segment(c + sep, "ins"))
        else:  # replace — 항목 개수가 안 맞거나 내용이 부분적으로만 겹침
            old_join = "".join(c + sep for c, sep in items_a[i1:i2])
            new_join = "".join(c + sep for c, sep in items_b[j1:j2])
            l2, r2 = _diff_plain(tokenize(old_join), tokenize(new_join))
            left.extend(l2)
            right.extend(r2)
    return left, right


def _diff_plain(a: list[str], b: list[str]) -> tuple[list[Segment], list[Segment]]:
    """기존 어절 단위 SequenceMatcher 비교(목록이 아니거나 항목 내부)."""
    sm = SequenceMatcher(None, a, b, autojunk=False)
    left: list[Segment] = []
    right: list[Segment] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old_part = "".join(a[i1:i2])
        new_part = "".join(b[j1:j2])
        if tag == "equal":
            left.append(Segment(old_part, "eq"))
            right.append(Segment(new_part, "eq"))
        elif tag == "delete":
            left.append(Segment(old_part, "del"))
        elif tag == "insert":
            right.append(Segment(new_part, "ins"))
        else:  # replace
            left.append(Segment(old_part, "del"))
            right.append(Segment(new_part, "ins"))
    return left, right


def _merge(segs: list[Segment]) -> list[Segment]:
    """같은 종류가 연달아 나오면 합침."""
    out: list[Segment] = []
    for s in segs:
        if not s.text:
            continue
        if out and out[-1].kind == s.kind:
            out[-1] = Segment(out[-1].text + s.text, s.kind)
        else:
            out.append(s)
    return out


def changed_words(old: str, new: str) -> tuple[list[str], list[str]]:
    """변경된 어절만 (삭제분, 추가분) 으로 뽑음. 공백은 제외."""
    left, right = diff_segments(old, new)
    dels = [w for s in left if s.kind == "del" for w in s.text.split() if w]
    inss = [w for s in right if s.kind == "ins" for w in s.text.split() if w]
    return dels, inss


def similarity(old: str, new: str) -> float:
    """어절 단위 유사도 0.0~1.0."""
    a, b = [t for t in tokenize(old) if t.strip()], [t for t in tokenize(new) if t.strip()]
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def change_ratio(old: str, new: str) -> float:
    """전체 대비 바뀐 글자 비율 0.0~1.0.

    유사도만으로는 긴 조문의 작은 변경과 짧은 조문의 큰 변경이
    구분되지 않아 함께 봄.
    """
    dels, inss = changed_words(old, new)
    changed = sum(len(w) for w in dels) + sum(len(w) for w in inss)
    total = len((old or "").replace(" ", "")) + len((new or "").replace(" ", ""))
    return changed / total if total else 0.0
