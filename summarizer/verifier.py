"""검증 — 조문 요약이 원문 사실과 맞는지 코드로 대조한다.

★ 왜 코드 검증인가 (2026-07-25 결정):
    LLM 에게 "틀린 곳을 찾아라"고 시키면, 정확한 요약에서도 트집을 만든다
    ("덜 다뤘다", "충분히 반영 안 함"). 프롬프트·필터로 눌러도 표현을
    바꿔가며 우회한다 — 키워드 두더지잡기라 끝이 없다.

    검증의 두 유형(환각·방향오류)은 실은 코드로 판정 가능하다:
        환각    — 요약이 말한 핵심어가 원문(old+new) 어디에도 없다.
        방향오류 — 요약이 'A→B'라는데 원문은 A가 old 가 아니라 new 에 있다.
    정답이 코드에 있으므로 오탐이 날 수 없다. LLM 을 판단자로 쓰지 않는다.

★ 무엇을 검증하나:
    조문 요약이 diff/원문과 어긋나는 '사실 오류'만. 요약이 짧은지 긴지,
    취지를 잘 잡았는지는 검증하지 않는다(사실이 아니라 취향).

    규칙 기반 요약(이동·변경없음)은 애초에 코드가 만든 확정 문장이라
    검증 대상이 아니다. LLM 이 문장을 쓴 것(개정·신설·통째교체·이동후개정)만
    본다.
"""

from __future__ import annotations

import re
from typing import Sequence

from summarizer.models import ArticleSummary, VerifierIssue

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _quoted_fragments(text: str) -> list[str]:
    """요약이 작은따옴표로 인용한 조각들을 '순서대로 쌍'으로 뽑는다.

    ★ 버그 주의(2026-07-25): "'등'이 추가되고, '때'가 '경우'로" 처럼 따옴표
      쌍이 여러 개인 문장에서, 단순 정규식 '([^']+)' 은 1번 닫음~2번 여는
      따옴표 사이("이 추가되고, ")를 잘못 인용어로 잡는다. 따옴표를 등장
      순서대로 짝(1-2, 3-4, ...)지어야 진짜 인용어만 나온다.
    """
    marks = [m.start() for m in re.finditer(r"['‘’]", text)]
    frags = []
    for k in range(0, len(marks) - 1, 2):  # 0-1, 2-3, ... 쌍으로
        frag = text[marks[k] + 1 : marks[k + 1]].strip()
        if frag:
            frags.append(frag)
    return frags


def _check_article(s: ArticleSummary) -> list[VerifierIssue]:
    """조문 요약 하나를 원문과 대조한다."""
    u = s.unit
    if s.error or not (s.summary or "").strip():
        return [VerifierIssue("high", u.location_label, "조문 요약 생성 실패")]

    old_n, new_n = _norm(u.old_text), _norm(u.new_text)
    src_n = old_n + new_n
    summary = s.summary
    issues: list[VerifierIssue] = []

    # ① 환각 검사 — 요약이 작은따옴표로 인용한 조각은 원문에 실제로 있어야 한다.
    #    diff 를 그대로 옮긴 것이므로, 없으면 요약이 지어낸 것이다.
    for frag in _quoted_fragments(summary):
        if _norm(frag) and _norm(frag) not in src_n:
            issues.append(
                VerifierIssue(
                    "high", u.location_label,
                    f"요약이 인용한 '{frag}'가 원문에 없음(환각 가능)",
                )
            )

    # ② 방향오류 검사 — 요약이 "A에서/가 B로 변경"이라 하면, 실제 diff 는
    #    "A → B" 여야 정방향이다. 요약이 diff 와 반대로("B → A") 썼으면
    #    방향오류다. diff 조각(코드가 만든 정답)과 요약을 대조한다.
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
                # 한쪽이 다른 쪽을 포함하면 같은 대상으로 본다
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

    return issues


def verify_summaries(summaries: Sequence[ArticleSummary]) -> list[VerifierIssue]:
    """법령의 모든 조문 요약을 검증한다. LLM 미개입 — 코드 대조만."""
    issues: list[VerifierIssue] = []
    for s in summaries:
        u = s.unit
        # 규칙 기반 요약은 코드가 만든 확정 문장이라 검증 불필요.
        if u.move_is_identical or u.no_change:
            continue
        issues.extend(_check_article(s))
    return issues
