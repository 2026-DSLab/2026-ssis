"""summarizer.matching.describe_change() 회귀 테스트.

이전엔 테스트가 하나도 없었음. 실측 버그를 계기로 추가함:
글자단위 diff가 숫자 하나만 다른 단어("20일"→"30일")를 단위 없는 맨
숫자("2"→"3")로 쪼개, 그 조각만 받은 LLM이 없는 맥락을 지어내는
사고(할루시네이션)로 이어졌음.
"""

from __future__ import annotations

from summarizer.matching import describe_change


def test_describe_change_keeps_whole_word_when_only_a_digit_differs():
    """실측(국가를 당사자로 하는 계약에 관한 법률 제28조②):
    글자단위 SequenceMatcher는 "20일"/"30일"에서 앞 숫자만 다르다고 보고
    "'2' → '3'"만 뽑았음. 단위(일)를 잃은 맨 숫자 조각을 받은 LLM이
    "당사자의 수가 2에서 3으로 변경"처럼 완전히 없는 내용을 지어냈음.
    어절 단위로 바꿔 "20일"→"30일" 전체가 한 조각으로 나와야 함.
    """
    old = (
        "이의신청은 이의신청의 원인이 되는 행위가 있었던 날부터 20일 이내 "
        "또는 그 행위가 있음을 안 날부터 15일 이내에 해당 중앙관서의 "
        "장에게 하여야 한다."
    )
    new = (
        "이의신청은 이의신청의 원인이 되는 행위가 있었던 날부터 30일 이내 "
        "또는 그 행위가 있음을 안 날부터 25일 이내에 해당 중앙관서의 "
        "장에게 하여야 한다."
    )

    parts = describe_change(old, new)

    assert parts == ["'20일' → '30일'", "'15일' → '25일'"]
    # 단위 없는 맨 숫자 조각은 다시는 나오면 안 됨.
    assert "'2' → '3'" not in parts
    assert "'1' → '2'" not in parts


def test_describe_change_single_word_swap():
    """실측(국민체육진흥법 제21조): 단어 하나만 바뀌면 그 단어(+조사)만 조각으로.

    tokenize()는 공백 기준 어절 단위라, 조사("는")는 앞 명사에 붙은 채
    하나의 토큰임 — 한국어에 조사 앞에 공백을 두지 않기 때문임.
    """
    old = "대한올림픽위원회는 이 법에 따른 업무를 수행한다."
    new = "대한체육회는 이 법에 따른 업무를 수행한다."

    parts = describe_change(old, new)

    assert parts == ["'대한올림픽위원회는' → '대한체육회는'"]


def test_describe_change_returns_empty_for_wholesale_replacement():
    """무관한 두 문장(통째 교체, 유사도 사실상 0)은 조각으로 쪼개지 않고
    빈 목록을 돌려줌 — diff_is_meaningful()이 False를 돌려주는 경로."""
    old = "가나다라마바사아자차"
    new = "카타파하거너더러머버"

    assert describe_change(old, new) == []


def test_describe_change_merges_word_that_moved_within_a_short_span():
    """실측(전기사업법 제7조의3①): "미리"라는 단어가
    "제3항에 따라 미리 관계 행정기관의 장과" → "인ㆍ허가등의 관계
    행정기관의 장과 미리"처럼 문장 안에서 자리만 옮기면, 어절 단위
    SequenceMatcher가 이걸 "이동"으로 못 보고 replace+insert 두 조각으로
    쪼갰다. 그 결과를 받은 LLM이 "인ㆍ허가등의 미리를 추가하였습니다"
    같은 비문을 만들었음. 사이에 낀 안 바뀐 어절이 짧으면(3어절 이하)
    하나의 완결된 치환으로 합쳐야 함.
    """
    old = "① 관하여 제3항에 따라 미리 관계 행정기관의 장과 협의한 사항에 대해서는 본다."
    new = "① 관하여 인ㆍ허가등의 관계 행정기관의 장과 미리 협의한 사항에 대해서는 본다."

    parts = describe_change(old, new)

    assert parts == ["'제3항에 따라 미리 관계 행정기관의 장과' → '인ㆍ허가등의 관계 행정기관의 장과 미리'"]


def test_describe_change_does_not_merge_unrelated_changes_far_apart():
    """실측(국가를 당사자로 하는 계약에 관한 법률 제28조②): "20일"→"30일"과
    "15일"→"25일"은 서로 무관한 별개 변경인데, 사이에 낀 안 바뀐 어절이
    7개나 됨. 이런 먼 변경까지 하나로 합치면 안 됨 — 조각이 흐려져
    LLM이 다시 맥락을 잃음."""
    old = (
        "이의신청은 이의신청의 원인이 되는 행위가 있었던 날부터 20일 이내 "
        "또는 그 행위가 있음을 안 날부터 15일 이내에 해당 중앙관서의 "
        "장에게 하여야 한다."
    )
    new = (
        "이의신청은 이의신청의 원인이 되는 행위가 있었던 날부터 30일 이내 "
        "또는 그 행위가 있음을 안 날부터 25일 이내에 해당 중앙관서의 "
        "장에게 하여야 한다."
    )

    parts = describe_change(old, new)

    assert parts == ["'20일' → '30일'", "'15일' → '25일'"]


def test_describe_change_dedupes_identical_fragments():
    """실측(산업재해보상보험법 제116조③): 같은 조각이 두 곳에 삽입되면
    "(N곳)"으로 합쳐서 LLM이 중복을 문장 내용으로 착각하지 않게 함."""
    old = "증명을 생략할 수 있다. 다만 증명을 생략할 수 있다."
    new = "증명 또는 자료의 제공을 생략할 수 있다. 다만 증명 또는 자료의 제공을 생략할 수 있다."

    parts = describe_change(old, new)

    assert any("(2곳)" in p for p in parts)
