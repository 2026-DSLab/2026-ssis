"""summarizer.loader.caveats_for() 회귀 테스트.

★★★ 설계(2026-07-31, 사용자 결정): match_status 기반 "확인이 필요한
항목" 캐비어트를 전부 없앴다. 하루 동안 이 함수를 여러 번 좁혀봤지만
(삭제 확실 케이스 제외 → 구조확장 확정 케이스 제외 → 위치재배치의심도
content_ratio 높으면 제외 → 신설이면 무조건 제외) 매번 "이것도 사실
문제없는데?"인 실측 사례가 계속 나왔다(예: 산업재해보상보험법 제127조④
— 옛 내용이 실은 새 항목 1.으로 그대로 옮겨갔는데도 부모 항목이 계속
의심 표시로 남음). "위치재배치의심" 자체가 "과잉 표시가 거짓 확정보다
안전하다"는 원칙으로 설계돼(src/lawtrack/db/repo.py:_reshuffled_articles)
노이즈가 구조적으로 신호보다 많다고 판단해, 이 경고 자체를 신뢰할 만한
신호로 안 쓰기로 했다. 이 테스트는 그 최종 결정(항상 빈 목록)을 고정한다
— match_status가 뭐든 절대 캐비어트를 내면 안 된다.
"""

from __future__ import annotations

from summarizer.loader import caveats_for
from summarizer.models import ArticleUnit


def _unit(**kw) -> ArticleUnit:
    base = dict(
        law_id="000000", law_name="테스트법", location_label="제1조",
        change_type="개정", old_text="old", new_text="new", match_status="성공",
    )
    base.update(kw)
    return ArticleUnit(**base)


def test_success_status_has_no_caveat():
    assert caveats_for(_unit(match_status="성공", old_text_is_context=False)) == []


def test_deletion_skip_status_has_no_caveat():
    unit = _unit(change_type="삭제", match_status="삭제(위치탐색제외)", old_text_is_context=True)

    assert caveats_for(unit) == []


def test_structural_expansion_status_has_no_caveat():
    unit = _unit(change_type="신설", match_status="구조확장(구법미분리)", old_text_is_context=True)

    assert caveats_for(unit) == []


def test_position_reshuffle_suspicion_has_no_caveat_even_when_content_differs():
    """★★★ 실측(2026-07-31, 산업재해보상보험법 제127조④, 사용자 결정):
    old/new 내용 유사도가 낮아 "진짜 의심스러워 보이는" 케이스여도, 실제
    사연(옛 내용이 형제 신설 항목으로 그대로 옮겨감)을 확인해보면 대부분
    무해했다. 유사도와 무관하게 위치재배치의심은 이제 캐비어트를 내지
    않는다."""
    unit = _unit(
        match_status="위치재배치의심", old_text_is_context=True,
        old_text="④ 제21조제3항을 위반하여 비밀을 누설한 자는 2년 이하의 징역 또는 1천만원 이하의 벌금에 처한다.",
        new_text="④ 다음 각 호의 어느 하나에 해당하는 자는 2년 이하의 징역 또는 1천만원 이하의 벌금에 처한다.",
    )

    assert caveats_for(unit) == []


def test_new_creation_never_flagged_even_with_reshuffle_status():
    unit = _unit(
        change_type="신설", match_status="위치재배치의심", old_text_is_context=True,
        old_text="", new_text="완전히 새로 생긴 조항 내용",
    )

    assert caveats_for(unit) == []
