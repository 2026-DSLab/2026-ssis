"""summarizer/locfmt.py 단위 테스트.

★ 2026-08-03: report/builder.py(HWPX)와 agents.py("번호만 이동" 규칙
기반 문장)가 같은 위치 표기 규칙을 공유하게 되면서 이 모듈로 뽑혔다.
"""

from summarizer.locfmt import article_of, format_location, format_position, position_of


def test_article_of_splits_off_article_number():
    assert article_of("제9조⑥1.가.") == "제9조"
    assert article_of("제10조의2②12의2.") == "제10조의2"


def test_article_of_returns_whole_string_when_no_article_prefix():
    """이동 전 위치처럼 조 번호 없이 항/호만 오는 값은 그대로 돌려준다
    (position_of가 이 경우 빈 문자열을 내도록 하는 전제)."""
    assert article_of("③1.") == "③1."


def test_position_of_returns_remainder_after_article():
    assert position_of("제9조⑥1.가.") == "⑥1.가."
    assert position_of("제9조") == ""


def test_position_of_empty_when_no_article_prefix():
    assert position_of("③1.") == ""


def test_format_position_adds_clause_item_subitem_suffixes():
    assert format_position("⑥1.가.") == "⑥항·1호·가목"


def test_format_position_handles_branch_numbered_item():
    """실측(2026-07-31, 국고금관리법 제10조의2②12의2.): "12의2." 같은
    호가지번호는 "의N"까지 통째로 하나의 조각이어야 한다."""
    assert format_position("②12의2.") == "②항·12의2호"


def test_format_position_empty_input_returns_empty():
    assert format_position("") == ""


def test_format_location_combines_article_and_position_with_space():
    assert format_location("제8조②12.") == "제8조 ②항·12호"


def test_format_location_handles_position_only_value():
    """조 번호 없이 위치만 있는 값(같은 조 안에서 옮겨진 이동 전 위치
    등)도 같은 규칙으로 항/호/목만 포맷해야 한다."""
    assert format_location("②5.") == "②항·5호"


def test_format_location_article_only_no_position():
    assert format_location("제8조") == "제8조"


def test_format_location_empty_or_none_passthrough():
    assert format_location("") == ""
    assert format_location(None) == ""
