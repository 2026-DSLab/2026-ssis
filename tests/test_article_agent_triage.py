"""ArticleAgent가 사전 선별(triage) 결과에 따라 LLM 호출을 실제로
건너뛰는지 검증한다.

summarizer/agents.py의 ArticleAgent.run()에 summarizer/triage.triage()를
이식(2026-07-30, seongbeen2 브랜치)했는데, triage() 자체가 옳게 판단해도
ArticleAgent가 그 판단을 실제로 LLM 스킵에 연결하지 않으면(배선 실수)
아무 효과가 없다 — 이 파일은 그 배선을 확인한다.
"""

from __future__ import annotations

from summarizer.agents import ArticleAgent
from summarizer.config import LLMSettings
from summarizer.models import ArticleUnit


class _CountingClient:
    """호출 여부만 세는 가짜 LLMClient. 실제로 불리면 안 되는 경로에서
    불렸는지를 이걸로 확인한다."""

    def __init__(self):
        self.calls = 0

    def complete_text(self, *, system, user, max_tokens, thinking=False):
        self.calls += 1
        return "LLM 응답"

    def complete_json(self, *, system, user, schema, max_tokens, thinking=False):
        self.calls += 1
        return {}


def _unit(**kw) -> ArticleUnit:
    base = dict(
        law_id="000000", law_name="테스트법", location_label="제1조",
        change_type="개정", old_text="old", new_text="new", match_status="성공",
    )
    base.update(kw)
    return ArticleUnit(**base)


def _agent(client) -> ArticleAgent:
    return ArticleAgent(client, LLMSettings(api_key="dummy"))


def test_formal_change_skips_llm_call():
    """기관명만 바뀐 조문은 LLM을 부르지 않고 규칙 기반 요약을 낸다."""
    client = _CountingClient()
    unit = _unit(
        old_text="환경부장관은 매년 기본계획을 수립하여야 한다.",
        new_text="기후에너지환경부장관은 매년 기본계획을 수립하여야 한다.",
    )
    result = _agent(client).run(unit)

    assert client.calls == 0
    assert "정비" in result.summary
    assert result.error is None


def test_no_change_at_raw_text_level_skips_llm_call():
    """unit.no_change 플래그가 없어도(계약 단계에서 놓친 경우), old/new
    본문 자체가 완전히 동일하면 triage가 잡아 LLM을 부르지 않는다."""
    client = _CountingClient()
    text = "1. 주요재료비    계약목적물의 기본적 구성형태를 이루는 물품의 가치"
    unit = _unit(old_text=text, new_text=text, no_change=False)

    result = _agent(client).run(unit)

    assert client.calls == 0
    assert "바뀌지 않았습니다" in result.summary


def test_substantive_change_still_calls_llm():
    """실질 변경은 여전히 LLM을 부른다 — triage가 전부를 막아서는 안 된다."""
    client = _CountingClient()
    unit = _unit(
        old_text="① 신청은 30일 이내에 하여야 한다.",
        new_text="① 신청은 60일 이내에 하여야 하며, 부득이한 경우 연장할 수 있다.",
    )
    result = _agent(client).run(unit)

    assert client.calls == 1
    assert result.summary == "LLM 응답"


def test_old_text_is_context_bypasses_triage_even_if_texts_look_formal():
    """old_text가 이 위치의 실제 개정 전 문장이 아니라 참고 맥락뿐인 경우
    (구조확장/위치재배치의심)는, 설사 두 문장이 기관명 정비처럼 보여도
    triage 자체를 적용하면 안 된다 — 애초에 같은 위치를 가리키지 않는
    문장끼리 비교하는 것이기 때문이다. 이때는 항상 LLM 경로로 간다."""
    client = _CountingClient()
    unit = _unit(
        old_text="환경부장관은 매년 기본계획을 수립하여야 한다.",
        new_text="기후에너지환경부장관은 매년 기본계획을 수립하여야 한다.",
        match_status="위치재배치의심",
        old_text_is_context=True,
    )
    result = _agent(client).run(unit)

    assert client.calls == 1
    assert result.summary == "LLM 응답"
