"""summarizer.agents._describe_formal_change() 조사(助詞) 회귀 테스트.

기존엔 테스트가 하나도 없었음. 실측 사고(대규모 워치리스트
재처리 검증 중 발견): deleted_words/inserted_words는 원문 문장 속에서
diff로 뽑힌 어절이라 이미 그 자리에 맞는 조사가 붙어 있는데, 함수가
조사를 새로 계산해 덧붙이면서 "환경부장관은이", "통계청이가" 같은
조사 중복 비문이 나왔음.
"""

from __future__ import annotations

from summarizer.agents import _describe_formal_change, _strip_trailing_josa


def test_strip_trailing_josa_removes_known_particles():
    assert _strip_trailing_josa("환경부장관은") == "환경부장관"
    assert _strip_trailing_josa("통계청이") == "통계청"
    assert _strip_trailing_josa("국가데이터처가") == "국가데이터처"
    assert _strip_trailing_josa("기후에너지환경부장관으로") == "기후에너지환경부장관"


def test_strip_trailing_josa_leaves_bare_noun_untouched():
    """조사가 안 붙은 순수 명사는 그대로 둠(오탈락 방지)."""
    assert _strip_trailing_josa("대한체육회") == "대한체육회"


def test_describe_formal_change_no_duplicated_josa_when_word_already_has_one():
    """실측(환경개선비용 부담법 제20조①): "환경부장관은"
    (이미 '은'이 붙어 있음)에 배치침 판정으로 뽑은 '이'를 또 붙여
    "환경부장관은이"라는 비문이 나왔음."""
    result = _describe_formal_change(["환경부장관은"], ["기후에너지환경부장관은"])

    assert "은이" not in result
    assert result == "환경부장관이 기후에너지환경부장관으로 변경되는 등 기관명·인용 법령명이 정비되었습니다."


def test_describe_formal_change_no_duplicated_josa_ga():
    """실측(국민기초생활보장법 제6조의2①): "통계청이"+
    "국가데이터처가"에 조사를 또 붙여 "통계청이가"/"국가데이터처가로"가
    나왔음."""
    result = _describe_formal_change(["통계청이"], ["국가데이터처가"])

    assert "이가" not in result
    assert "가로" not in result
    assert result == "통계청이 국가데이터처로 변경되는 등 기관명·인용 법령명이 정비되었습니다."


def test_describe_formal_change_strips_possessive_josa_too():
    """실측(환경개선비용 부담법 제22조 "환경부장관의 권한은"):
    소유격 '의'가 붙은 채로 뽑히면 "환경부장관의가"라는 비문이 나왔음."""
    result = _describe_formal_change(["환경부장관의"], ["기후에너지환경부장관의"])

    assert "의가" not in result
    assert "의로" not in result
    assert result == "환경부장관이 기후에너지환경부장관으로 변경되는 등 기관명·인용 법령명이 정비되었습니다."


def test_describe_formal_change_bare_nouns_still_get_a_particle():
    """실측(국민체육진흥법 제21조, 조사 없는 순수 명사 diff): 이 경우엔
    조사가 원래 없었으므로 정상적으로 하나 붙어야 함."""
    result = _describe_formal_change(["대한올림픽위원회"], ["대한체육회"])

    assert result == "대한올림픽위원회가 대한체육회로 변경되는 등 기관명·인용 법령명이 정비되었습니다."


def test_describe_formal_change_empty_falls_back_to_generic_sentence():
    assert _describe_formal_change([], []) == "기관명·인용 법령명이 정비되었습니다."
