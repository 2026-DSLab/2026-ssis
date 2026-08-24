"""검증 — 조문 요약이 원문 사실과 맞는지 코드로 대조함.

왜 코드 검증인가 (결정):
    LLM 에게 "틀린 곳을 찾아라"고 시키면, 정확한 요약에서도 트집을 만듦
    ("덜 다뤘다", "충분히 반영 안 함"). 프롬프트·필터로 눌러도 표현을
    바꿔가며 우회함 — 키워드 두더지잡기라 끝이 없음.

    검증의 두 유형(환각·방향오류)은 실은 코드로 판정 가능함:
        환각    — 요약이 말한 핵심어가 원문(old+new) 어디에도 없음.
        방향오류 — 요약이 'A→B'라는데 원문은 A가 old 가 아니라 new 에 있음.
    정답이 코드에 있으므로 오탐이 날 수 없음. LLM 을 판단자로 쓰지 않음.

무엇을 검증하나:
    조문 요약이 diff/원문과 어긋나는 '사실 오류'만. 요약이 짧은지 긴지,
    취지를 잘 잡았는지는 검증하지 않음(사실이 아니라 취향).

    규칙 기반 요약(이동·변경없음)은 애초에 코드가 만든 확정 문장이라
    검증 대상이 아님. LLM 이 문장을 쓴 것(개정·신설·통째교체·이동후개정)만
    봄.
"""

from __future__ import annotations

import re
from typing import Sequence

from summarizer.models import ArticleSummary, VerifierIssue

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")


def _numbers_in(text: str) -> set[str]:
    """텍스트에 나오는 숫자만 정규화(쉼표 제거)해서 뽑음.

    실측(영유아보육법 제8조②9.): "9. 영유아 보육ㆍ교육..."
    처럼 항목 번호 뒤의 "."은 소수점이 아니라 목록 구분자인데, 정규식이
    "9."까지 하나의 숫자로 통째로 잡았음. 그런데 요약 문장 "9번 항목으로
    옮겨왔습니다"에서는 "9" 뒤에 "번"이 와서 "9"만 잡힌다 — "9." ≠ "9"라
    같은 숫자인데도 다른 문자열로 보여 없는 숫자로 오탐(환각 아님)했음.
    뒤에 숫자가 안 이어지는 "."은 소수점이 아니므로 떼어냄("9.5"처럼
    뒤에 숫자가 있으면 진짜 소수점이라 그대로 둠 — \\d* 매치라 이미
    "9.5"를 통째로 잡으므로 이 케이스는 애초에 끝이 "."로 안 끝남).
    """
    out: set[str] = set()
    for m in _NUMBER_RE.finditer(text or ""):
        num = m.group(0).replace(",", "")
        if num.endswith("."):
            num = num[:-1]
        out.add(num)
    return out


def _quoted_fragments(text: str) -> list[str]:
    """요약이 작은따옴표로 인용한 조각들을 '순서대로 쌍'으로 뽑음.

    버그 주의: "'등'이 추가되고, '때'가 '경우'로" 처럼 따옴표
      쌍이 여러 개인 문장에서, 단순 정규식 '([^']+)' 은 1번 닫음~2번 여는
      따옴표 사이("이 추가되고, ")를 잘못 인용어로 잡음. 따옴표를 등장
      순서대로 짝(1-2, 3-4, ...)지어야 진짜 인용어만 나옴.
    """
    marks = [m.start() for m in re.finditer(r"['‘’]", text)]
    frags = []
    for k in range(0, len(marks) - 1, 2):  # 0-1, 2-3, ... 쌍으로
        frag = text[marks[k] + 1 : marks[k + 1]].strip()
        if frag:
            frags.append(frag)
    return frags


def _check_article(s: ArticleSummary) -> list[VerifierIssue]:
    """조문 요약 하나를 원문과 대조함."""
    u = s.unit
    if s.error or not (s.summary or "").strip():
        return [VerifierIssue("high", u.location_label, "조문 요약 생성 실패")]

    old_n, new_n = _norm(u.old_text), _norm(u.new_text)
    src_n = old_n + new_n
    summary = s.summary
    issues: list[VerifierIssue] = []

    # ① 환각 검사 — 요약이 작은따옴표로 인용한 조각은 원문에 실제로 있어야 함.
    #    diff 를 그대로 옮긴 것이므로, 없으면 요약이 지어낸 것임.
    for frag in _quoted_fragments(summary):
        if _norm(frag) and _norm(frag) not in src_n:
            issues.append(
                VerifierIssue(
                    "high", u.location_label,
                    f"요약이 인용한 '{frag}'가 원문에 없음(환각 가능)",
                )
            )

    # ② 방향오류 검사 — 요약이 "A에서/가 B로 변경"이라 하면, 실제 diff 는
    #    "A → B" 여야 정방향임. 요약이 diff 와 반대로("B → A") 썼으면
    #    방향오류임. diff 조각(코드가 만든 정답)과 요약을 대조함.
    if u.change_type in ("개정", "이동후개정") and u.diff_parts:
        # diff 의 정방향 치환쌍 목록: [(A, B), ...]
        diff_pairs = []
        for part in u.diff_parts:
            mm = re.match(r"['‘’](.+?)['‘’]\s*→\s*['‘’](.+?)['‘’]", part)
            if mm:
                diff_pairs.append((_norm(mm.group(1)), _norm(mm.group(2))))
        # 요약이 말한 치환쌍: "A에서/가 B로 (변경/바뀜/개정/수정/교체)"
        for a, b in re.findall(
            r"['‘’]?([\w가-힣ㆍ·]{1,30}?)['‘’]?\s*(?:에서|가|이)\s*['‘’]?([\w가-힣ㆍ·]{1,30}?)['‘’]?\s*(?:으로|로)\s*(?:변경|바뀌|개정|수정|교체)",
            summary,
        ):
            an, bn = _norm(a), _norm(b)

            def _rel(x: str, y: str) -> bool:
                # 한쪽이 다른 쪽을 포함하면 같은 대상으로 봄
                # (diff '통계청' vs 요약 '통계청장').
                return bool(x) and bool(y) and (x in y or y in x)

            for da, db in diff_pairs:
                forward = _rel(an, da) and _rel(bn, db)   # 요약 A→B = diff A→B (정방향)
                reverse = _rel(an, db) and _rel(bn, da)   # 요약 A→B = diff B→A (역방향!)
                if reverse and not forward:
                    issues.append(
                        VerifierIssue(
                            "high", u.location_label,
                            f"요약이 '{a}→{b}'라 했으나 실제로는 반대 방향(방향오류)",
                        )
                    )

    # ③ 숫자 환각 검사 — 인용부호 없이 자연스러운 문장으로 쓴 요약도
    #    검증함. ①의 인용부호 검사는 요약이 스스로 따옴표를 친 곳만
    #    보므로, "구체적으로/자연스럽게 쓰라"는 프롬프트를 따라 LLM이
    #    따옴표 없이 통짜 문장을 쓰면(권장되는 방식임) 그 안의 숫자는
    #    전혀 검증되지 않는 사각지대가 있었음.
    #
    #    실측(국가를 당사자로 하는 계약에 관한 법률
    #    제28조②): describe_change()가 "20일"→"30일"을 "'2'→'3'"으로
    #    쪼갠 버그 때문에 LLM이 "당사자의 수가 '2'에서 '3'으로 변경"이라는
    #    완전히 없는 내용을 지어냈음. 그 버그는 diff 단계에서 고쳤지만
    #    (숫자가 항상 단위와 함께 나오게), 이 검사는 그 근본 버그와
    #    무관하게 "요약에 등장한 숫자가 원문에 아예 없으면 위험 신호"라는
    #    일반 규칙이라 앞으로 비슷한 사고(다른 원인이더라도)를 잡아냄.
    #    숫자는 문장처럼 의역되지 않음 — 원문에 없는 숫자가 요약에
    #    있으면 예외 없이 지어낸 것임(오탐 위험이 없는 이유).
    if u.change_type in ("개정", "신설", "이동후개정"):
        src_numbers = _numbers_in(u.old_text) | _numbers_in(u.new_text)
        for num in _numbers_in(summary):
            if num not in src_numbers:
                issues.append(
                    VerifierIssue(
                        "high", u.location_label,
                        f"요약의 숫자 '{num}'가 원문에 없음(환각 가능)",
                    )
                )

    return issues


def verify_summaries(summaries: Sequence[ArticleSummary]) -> list[VerifierIssue]:
    """법령의 모든 조문 요약을 검증함. LLM 미개입 — 코드 대조만."""
    issues: list[VerifierIssue] = []
    for s in summaries:
        u = s.unit
        # 규칙 기반 요약은 코드가 만든 확정 문장이라 검증 불필요.
        if u.move_is_identical or u.no_change:
            continue
        issues.extend(_check_article(s))
    return issues
