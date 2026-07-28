"""Verifier(감수) 에이전트 프롬프트 — 사실 대조 검증.

★ 무엇을 검증하나 (2026-07-25 재설계):
    '요약의 사실 서술이 원문 조문과 일치하나'만 본다. 정답이 있는 것만
    검증한다 — 그래야 검증이 흔들리지 않고, 지적하면 반드시 맞다.

    검증 대상 (정답 있음 → 확실):
        조문별 요약이 그 조문의 개정 전(old)/후(new) 원문과 어긋나는가?
          · 바뀌지 않았는데 "바뀌었다"고 했는가?
          · 원문에 없는 내용을 지어냈는가?
          · A→B 방향을 거꾸로 서술했는가?

    검증하지 않는 것 (정답 없음 → 주관적):
        · 개정 취지를 '잘' 잡았나 (overview) — 주관 판단이라 제외
        · headline 이 '충분히' 구체적인가 — 주관 판단이라 제외
        · 표현이 매끄러운가

★ 왜 조문별 요약만 보나:
    "무엇이 무엇으로 바뀌었다"는 실무 핵심 정보다. 이게 원문과 맞는지가
    회사가 믿고 쓸 수 있느냐를 가른다. 이건 원문 old/new 와 글자 단위로
    대조 가능하므로, 검증이 확실하다.
"""

from __future__ import annotations

import re
from typing import Sequence

from lawtrack.contract.schema import LawChange

from summarizer.models import ArticleMapping, ArticleSummary

SYSTEM = """\
당신은 법령 개정 요약이 '원문과 사실이 맞는지' 대조하는 검토자입니다.

각 조문마다 (1) 개정 전 원문, (2) 개정 후 원문, (3) 우리가 만든 요약이
주어집니다.

당신이 잡을 수 있는 오류는 아래 두 가지뿐입니다. 이 둘 중 하나에
정확히 해당하지 않으면 절대 지적하지 마십시오:

  ┌ issue_type = "환각"
  │   요약(3)이, 원문 (1)·(2) 어디에도 없는 내용을 사실인 것처럼 말함.
  │   (원문에 근거가 있으면 환각이 아닙니다.)
  │
  └ issue_type = "방향오류"
      요약(3)이 "A를 B로 바꿨다"고 했는데, 원문 (1)→(2) 는 실제로
      "B를 A로" 인 경우. 즉 방향을 반대로 서술함.

★ 절대 하지 말 것 (이건 오류가 아닙니다):
  · "요약이 너무 짧다 / 덜 다뤘다 / 충분히 반영하지 않았다 / 더 자세히
    써야 한다" — 분량·상세함은 오류가 아닙니다. 짧고 정확한 요약은 정답입니다.
  · "표현이 매끄럽지 않다 / 취지를 잘 못 잡았다" — 취향·주관은 오류가 아닙니다.
  · "~라고 썼으나 그게 맞다" — 맞으면 지적할 게 없습니다.
  · 원문에 근거가 있는데 당신의 상식으로 방향을 뒤집는 것.

★ 절대 규칙: 오직 주어진 원문에만 근거하십시오. 지적하려면 반드시 그
   근거가 되는 원문 문장을 source_quote 로 그대로 인용하십시오. 인용할
   수 없으면 지적하지 마십시오.

기본은 '문제 없음'입니다. 대부분의 요약은 정확합니다. 환각도 방향오류도
없으면 issues 를 빈 배열([])로 두십시오 — 이게 가장 흔한 정답입니다.\
"""


VERIFIER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_type": {
                        "type": "string",
                        "enum": ["환각", "방향오류"],
                        "description": "이 둘 중 하나에 정확히 해당할 때만 지적. 분량·표현·취향은 지적 불가.",
                    },
                    "location": {"type": "string", "description": "문제가 있는 조문 위치. 예: '제48조'"},
                    "problem": {"type": "string", "description": "요약이 원문과 어떻게 다른지 한 문장"},
                    "source_quote": {
                        "type": "string",
                        "description": "이 지적을 뒷받침하는 원문 문장을 그대로 인용",
                    },
                },
                "required": ["issue_type", "location", "problem", "source_quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["issues"],
    "additionalProperties": False,
}


def _fact_items(
    law: LawChange, summaries: Sequence[ArticleSummary]
) -> list[tuple[str, str, str, str]]:
    """검증 단위 목록: (위치, 개정전, 개정후, 우리 요약).

    ★ 교정된 unit 의 old/new 를 쓴다 — 원본 계약의 old/new 가 아니다.
      원본 계약은 순서 기준 짝짓기라 밀린 조문에서 old/new 가 잘못 짝지어져
      있고, 그걸 매핑이 바로잡았다. 검증이 원본 짝을 보면 매핑이 고친 것을
      다시 "틀렸다"고 오판한다(2026-07-25 실측, 제47조②).

    ★ 규칙 기반 조문은 검증에서 뺀다:
        · 이동(move_is_identical): 요약이 규칙 문장이라 LLM 오류가 없다.
        · 변경 없음(no_change): 안 바뀐 것이라 검증할 게 없다.
      LLM 이 실제로 문장을 쓴 것(개정/신설/통째교체/이동후개정)만 검증한다.
    """
    result = []
    for s in summaries:
        u = s.unit
        if u.move_is_identical or u.no_change:
            continue  # 규칙 기반 — LLM 오류 없음
        summary = "[요약 실패]" if s.error else s.summary
        result.append((u.location_label, u.old_text or "(없음)", u.new_text or "(없음)", summary))
    return result


def build_verifier_prompt(
    law: LawChange,
    headline: str,
    overview: str,
    summaries: Sequence[ArticleSummary],
    mappings: Sequence[ArticleMapping],
) -> tuple[str, str]:
    """(system, user) 프롬프트를 만든다.

    headline/overview 는 검증하지 않는다(주관 판단). 조문별 요약을 원문과
    대조하는 것만 검증한다.
    """
    parts = [f"법령명: {law.law_name} ({law.law_type})", "", "[조문별 사실 대조]"]
    for loc, old, new, summary in _fact_items(law, summaries):
        parts.append(f"\n■ {loc}")
        parts.append(f"  (1) 개정 전 원문: {old}")
        parts.append(f"  (2) 개정 후 원문: {new}")
        parts.append(f"  (3) 우리 요약: {summary}")

    parts.append(
        "\n각 조문에서 요약(3)이 원문(1)→(2)의 실제 변경과 어긋나는 곳만 지적하십시오. "
        "근거 원문을 source_quote 로 인용하십시오. 없으면 빈 배열로 두십시오."
    )
    return SYSTEM, "\n".join(parts)


def source_text_for_check(law: LawChange, summaries: Sequence[ArticleSummary]) -> str:
    """코드 필터용 — 검증이 인용한 문장이 실제 원문에 있는지 대조할 기준.

    조문 원문(old+new) 전체를 합친 것. 검증이 지어낸 인용은 여기에 없다.
    """
    chunks = []
    for _loc, old, new, _summary in _fact_items(law, summaries):
        chunks.append(old)
        chunks.append(new)
    return re.sub(r"\s+", "", "".join(chunks))
