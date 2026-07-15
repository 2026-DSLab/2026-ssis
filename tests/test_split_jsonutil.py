"""split / jsonutil 테스트. 케이스는 전부 실측 사례 기반."""

from lawtrack.parse.jsonutil import (
    as_list,
    dig,
    dig_list,
    looks_like_api_error,
    text_of,
)
from lawtrack.text.split import (
    ArticleNo,
    Level,
    article_title,
    searchable_fragments,
    split_all,
    split_by_clause,
    split_by_item,
    split_by_subitem,
)


class TestClauseSplit:
    """항 분리 — 전자정부법/도로교통법 실패 사례."""

    def test_split_circled_markers(self):
        text = "① 첫째 항 내용이다 ② 둘째 항 내용이다 ③ 셋째 항 내용이다"
        frags = split_by_clause(text)
        assert len(frags) == 3
        assert [f.marker for f in frags] == ["①", "②", "③"]
        assert frags[0].text == "첫째 항 내용이다"

    def test_real_case_social_security_act(self):
        """사회보장기본법 제21조 실측 문장."""
        text = (
            "제21조(위원회의 구성 등) ① 위원회는 위원장 1명, 부위원장 3명과 "
            "행정안전부장관을 포함한 30명 이내의 위원으로 구성한다. "
            "② 위원장은 국무총리가 되고 부위원장은 교육부장관이 된다. "
            "③ ∼ ⑨ (생 략)"
        )
        frags = split_by_clause(text)
        markers = [f.marker for f in frags]
        assert "①" in markers and "②" in markers and "③" in markers

    def test_skippable_fragments_excluded(self):
        text = "① 실제 내용이 있는 항 ② (생 략) ③ (현행과 같음)"
        searchable = searchable_fragments(text)
        assert len(searchable) == 1
        assert "실제 내용" in searchable[0].text

    def test_no_marker_returns_whole(self):
        text = "기호가 하나도 없는 평범한 조문 내용"
        frags = split_by_clause(text)
        assert len(frags) == 1
        assert frags[0].level is Level.NONE
        assert frags[0].marker is None


class TestItemSplit:
    """호 분리."""

    def test_basic_items(self):
        text = "1. 첫째 호 내용 2. 둘째 호 내용 3. 셋째 호 내용"
        frags = split_by_item(text)
        assert [f.marker for f in frags] == ["1.", "2.", "3."]

    def test_branch_item(self):
        """실측: 아동복지법 '12의2. 제20조의3에 따른 …' 신설"""
        text = "12. 기존 호 내용 12의2. 제20조의3에 따른 후견인 선임 등에 대한 법률상담의 지원"
        frags = split_by_item(text)
        markers = [f.marker for f in frags]
        assert "12의2." in markers

    def test_article_reference_not_mistaken_for_item(self):
        """'제27조' 의 숫자를 호 기호로 오인하면 안 된다."""
        text = "「통계법」 제27조에 따라 국가데이터처가 공표하는 통계자료"
        frags = split_by_item(text)
        assert len(frags) == 1  # 쪼개지지 않아야 함


class TestSubItemSplit:
    def test_korean_markers(self):
        text = "가. 첫째 목 나. 둘째 목 다. 셋째 목"
        frags = split_by_subitem(text)
        assert [f.marker for f in frags] == ["가.", "나.", "다."]


class TestSplitAll:
    def test_hierarchical(self):
        text = "① 항 내용 ② 다음 각 호와 같다 1. 첫째 호 2. 둘째 호"
        frags = split_all(text)
        assert len(frags) >= 3

    def test_empty(self):
        assert split_all("") == []
        assert split_all("   ") == []


class TestArticleNo:
    """조문번호 6자리 인코딩 — 실측 확인된 규칙."""

    def test_from_code_with_branch(self):
        a = ArticleNo.from_code("000602")
        assert (a.number, a.branch) == (6, 2)
        assert a.label == "제6조의2"

    def test_from_code_plain(self):
        a = ArticleNo.from_code("002100")
        assert (a.number, a.branch) == (21, 0)
        assert a.label == "제21조"

    def test_roundtrip(self):
        for code in ("000602", "002100", "005400", "005606", "007100"):
            assert ArticleNo.from_code(code).to_code() == code

    def test_from_text(self):
        a = ArticleNo.from_text("제6조의2(기준 중위소득의 산정) ① 기준 중위소득은 …")
        assert a is not None and a.label == "제6조의2"

    def test_title_extract(self):
        assert article_title("제6조의2(기준 중위소득의 산정) ①") == "기준 중위소득의 산정"


class TestJsonUtil:
    def test_as_list_dict(self):
        """admrul 87% 케이스 — 단일 결과가 dict 로 온다."""
        assert as_list({"행정규칙명": "정보시스템 감리기준"}) == [
            {"행정규칙명": "정보시스템 감리기준"}
        ]

    def test_as_list_list(self):
        assert len(as_list([{"a": 1}, {"a": 2}])) == 2

    def test_as_list_none(self):
        assert as_list(None) == []

    def test_dig(self):
        data = {"법령": {"조문": {"조문단위": {"조문번호": "6"}}}}
        assert dig(data, "법령", "조문", "조문단위", "조문번호") == "6"

    def test_dig_through_list(self):
        data = {"법령": [{"조문": {"조문번호": "6"}}]}
        assert dig(data, "법령", "조문", "조문번호") == "6"

    def test_dig_default(self):
        assert dig({"a": {}}, "a", "b", default="-") == "-"

    def test_dig_list_normalizes(self):
        single = {"법령": {"조문": {"조문단위": {"조문번호": "6"}}}}
        multi = {"법령": {"조문": {"조문단위": [{"조문번호": "6"}, {"조문번호": "7"}]}}}
        assert len(dig_list(single, "법령", "조문", "조문단위")) == 1
        assert len(dig_list(multi, "법령", "조문", "조문단위")) == 2

    def test_text_of_strips_cdata_spaces(self):
        assert text_of(" 국민기초생활 보장법 ") == "국민기초생활 보장법"

    def test_text_of_wrapped(self):
        assert text_of({"#text": " 값 "}) == "값"


class TestApiErrorDetection:
    def test_real_error_response(self):
        """실측된 인증 실패 응답 — HTTP 200 + 유효 JSON 으로 온다."""
        err = {
            "result": "사용자 정보 검증에 실패하였습니다.",
            "msg": "OPEN API 호출 시 사용자 검증을 위하여 정확한 서버장비의 IP주소 및 도메인주소를 등록해 주세요.",
        }
        assert looks_like_api_error(err) is True

    def test_normal_response_not_error(self):
        ok = {"법령": {"기본정보": {"법령명_한글": "전자정부법"}, "조문": {}}}
        assert looks_like_api_error(ok) is False