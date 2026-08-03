"""summarizer.verifier 회귀 테스트.

기존엔 테스트가 하나도 없었다. 실측 사고(2026-07-31, 국가를 당사자로
하는 계약에 관한 법률 제28조②)를 계기로 추가한다: describe_change()가
"20일"→"30일"을 "'2'→'3'"으로 쪼갠 버그 때문에 LLM이 "당사자의 수가
'2'에서 '3'으로 변경"이라는 없는 내용을 지어냈다. 그 버그는 diff 단계
에서 고쳤지만, verifier가 인용부호 안 내용만 검증해서 따옴표 없는
자연스러운 문장 속 숫자 환각은 여전히 못 잡는 사각지대가 있었다 —
그 사각지대를 메우는 숫자 검사(③)를 검증한다.
"""

from __future__ import annotations

from summarizer.models import ArticleSummary, ArticleUnit
from summarizer.verifier import verify_summaries


def _unit(**kw) -> ArticleUnit:
    base = dict(
        law_id="000695", law_name="국가를 당사자로 하는 계약에 관한 법률",
        location_label="제28조②", change_type="개정",
        old_text="② 이의신청은 …20일 이내 또는 …15일 이내에 하여야 한다.",
        new_text="② 이의신청은 …30일 이내 또는 …25일 이내에 하여야 한다.",
        match_status="성공", diff_parts=["'20일' → '30일'", "'15일' → '25일'"],
    )
    base.update(kw)
    return ArticleUnit(**base)


def test_verify_flags_number_not_in_source_even_without_quotes():
    """★★ 실측 재현: 인용부호 없이 자연스러운 문장으로 써도, 원문에 없는
    숫자를 쓰면 잡아내야 한다."""
    summary = ArticleSummary(
        unit=_unit(),
        summary="당사자의 수가 2에서 3으로 변경되었으며, 계약의 종류가 1에서 2로 변경되었습니다.",
    )

    issues = verify_summaries([summary])

    assert any("환각" in i.problem for i in issues)


def test_verify_passes_when_numbers_match_source():
    """올바른 요약(원문에 있는 숫자만 씀)은 숫자 검사에 걸리지 않는다."""
    summary = ArticleSummary(
        unit=_unit(),
        summary="이의신청 기간이 20일에서 30일로, 15일에서 25일로 각각 연장되었습니다.",
    )

    issues = verify_summaries([summary])

    assert issues == []


def test_verify_number_check_ignores_rule_based_summaries():
    """이동(move_is_identical)·변경없음(no_change)은 규칙 기반 문장이라
    검증 대상이 아니다 — 숫자 검사도 예외 없이 건너뛰어야 한다."""
    unit = _unit(
        change_type="이동", move_is_identical=True,
        diff_parts=[], old_text="① 100분의 5", new_text="① 100분의 5",
    )
    summary = ArticleSummary(unit=unit, summary="999라는 원문에 없는 숫자")

    issues = verify_summaries([summary])

    assert issues == []


def test_verify_number_check_ignores_list_marker_dot():
    """★★ 실측(2026-07-31, 영유아보육법 제8조②9.): 새 항목 번호 "9. 영유아
    보육..."의 "."은 소수점이 아니라 목록 구분자인데, 정규식이 "9."까지
    통째로 숫자로 잡았다. 요약 문장 "9번 항목으로 옮겨왔습니다"의 "9"는
    뒤에 "번"이 와서 점 없이 "9"만 잡혀, "9." ≠ "9"로 오탐(가짜 환각
    경보)했다. 목록 번호 뒤의 "."은 무시하고 같은 숫자로 봐야 한다."""
    unit = _unit(
        location_label="제8조②9.", change_type="이동후개정",
        old_text="8. 영유아 보육 관련 교육 및 홍보에 관한 업무",
        new_text="9. 영유아 보육ㆍ교육 관련 교육 및 홍보에 관한 업무",
        diff_parts=["'보육' → '보육ㆍ교육'"],
    )
    summary = ArticleSummary(
        unit=unit,
        summary="'영유아 보육'이라는 항목이 '영유아 보육ㆍ교육'으로 변경되어 9번 항목으로 옮겨왔습니다.",
    )

    issues = verify_summaries([summary])

    assert issues == []


def test_verify_still_flags_quoted_hallucination():
    """기존 인용부호 검사(①)가 이번 변경으로 깨지지 않았는지 확인."""
    unit = _unit(old_text="'대한올림픽위원회'는 ...", new_text="'대한체육회'는 ...")
    summary = ArticleSummary(unit=unit, summary="'문화체육관광부'가 새로 추가되었습니다.")

    issues = verify_summaries([summary])

    assert any("환각" in i.problem for i in issues)
