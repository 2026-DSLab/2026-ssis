"""summarizer.loader._units_from_expansion() 내용 대조 매칭 회귀 테스트.

구조확장(옛 프로즈 문장이 목 단위로 안 나뉘어 있던
경우) 그룹에서, 새 항목 문구가 옛 프로즈 안에 그대로(부분문자열로) 남아
있으면 match_status="성공"으로 확정하고, 없으면(진짜 새 내용) 예전처럼
"구조확장(구법미분리)"로 미확정 남김.

실측 원본(전자정부법 제2조11. "정보자원" 정의)을 그대로 고정값으로 씀.
"""

from __future__ import annotations

from lawtrack.contract.schema import ExpandedItem, LawChange, StructuralExpansion

from summarizer.loader import _units_from_expansion

_OLD_TEXT = (
    "11. \"정보자원\"이란 행정기관등이 보유하고 있는 행정정보, 전자적 수단에 "
    "의하여 행정정보의 수집ㆍ가공ㆍ검색을 하기 쉽게 구축한 정보시스템, "
    "정보시스템의 구축에 적용되는 정보기술, 정보화예산 및 정보화인력 등을 말한다."
)


def _law(group: StructuralExpansion) -> LawChange:
    return LawChange(
        law_id="009199", new_serial_no="000000", law_name="전자정부법",
        law_type="법률", enforce_date="2026-01-01", revision_type="일부개정",
        articles=[], structural_expansions=[group],
    )


def test_items_preserved_in_old_prose_are_resolved_as_success():
    """실측(전자정부법 제2조11.): 옛 프로즈 안에 그대로
    남아있는 "행정정보"/"정보시스템"/"정보시스템의 구축에 적용되는
    정보기술" 3개 항목은 match_status="성공"으로 확정되어야 한다."""
    group = StructuralExpansion(
        article_label="제2조", old_text=_OLD_TEXT,
        new_items=[
            ExpandedItem(item_label="가.", text="가. 행정정보"),
            ExpandedItem(item_label="나.", text="나. 정보시스템"),
            ExpandedItem(item_label="다.", text="다. 정보시스템의 구축에 적용되는 정보기술"),
        ],
    )
    law = _law(group)

    units = _units_from_expansion(law, group)

    assert [u.match_status for u in units] == ["성공", "성공", "성공"]
    assert [u.old_text_is_context for u in units] == [False, False, False]
    assert units[0].old_text == "행정정보"
    assert units[1].old_text == "정보시스템"
    assert units[2].old_text == "정보시스템의 구축에 적용되는 정보기술"


def test_genuinely_new_item_stays_unresolved():
    """실측: "정보시스템 운영시설" 관련 항목("라")은 옛 프로즈 어디에도
    없는 완전히 새 내용이라, 매칭하면 안 되고 예전처럼 미확정으로 남아야
    함."""
    group = StructuralExpansion(
        article_label="제2조", old_text=_OLD_TEXT,
        new_items=[
            ExpandedItem(
                item_label="라.",
                text="라. 정보시스템의 운영에 필요한 건축물 및 건축설비(이하 \"정보시스템 운영시설\"이라 한다)",
            ),
        ],
    )
    law = _law(group)

    units = _units_from_expansion(law, group)

    assert units[0].match_status == "구조확장(구법미분리)"
    assert units[0].change_type == "신설"
    assert units[0].old_text_is_context is True
    assert units[0].old_text == _OLD_TEXT  # 그룹 전체가 참고 맥락으로 그대로 붙는다


def test_match_ignores_whitespace_differences():
    """실측: 옛 프로즈엔 "정보화예산"(공백 없음), 새 항목엔 "정보화 예산"
    (공백 있음)으로 나옴 — 공백 차이만으로 매칭을 놓치면 안 됨."""
    group = StructuralExpansion(
        article_label="제2조", old_text=_OLD_TEXT,
        new_items=[ExpandedItem(item_label="마.", text="마. 정보화 예산")],
    )
    law = _law(group)

    units = _units_from_expansion(law, group)

    assert units[0].match_status == "성공"
    assert units[0].old_text == "정보화예산"


def test_short_match_below_threshold_is_not_confirmed():
    """오탐 방지: 정규화 길이가 너무 짧은 조각(예: 2글자)은 우연히 걸릴
    위험이 크므로 확정하지 않음."""
    group = StructuralExpansion(
        article_label="제2조", old_text="1. 정보 관련 여러 사항을 말한다.",
        new_items=[ExpandedItem(item_label="가.", text="가. 정보")],
    )
    law = _law(group)

    units = _units_from_expansion(law, group)

    assert units[0].match_status == "구조확장(구법미분리)"


def test_two_items_do_not_claim_the_same_old_fragment_twice():
    """안전장치: 같은 옛 조각을 두 새 항목이 중복해서 근거로 삼지 않음."""
    group = StructuralExpansion(
        article_label="제2조", old_text="1. 정보시스템 하나만 언급한다.",
        new_items=[
            ExpandedItem(item_label="가.", text="가. 정보시스템"),
            ExpandedItem(item_label="나.", text="나. 정보시스템"),
        ],
    )
    law = _law(group)

    units = _units_from_expansion(law, group)

    statuses = [u.match_status for u in units]
    assert statuses.count("성공") == 1
    assert statuses.count("구조확장(구법미분리)") == 1
