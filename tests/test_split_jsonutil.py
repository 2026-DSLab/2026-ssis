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

    def test_section_reference_not_mistaken_for_item(self):
        """★★ 실측(2026-07-16, 조달청 협상에 의한 계약 제안서평가 세부기준
        제9조①): 다른 문서의 장/절/관/편 하위번호를 가리키는 참조 표현
        ("제7장 제3절의 4.(제안서의 평가)")이 이 조문 자체의 호 번호로
        오인되면 안 된다. 실제로는 단어 하나("기획재정부"→"재정경제부")만
        바뀐 안 쪼개져도 될 문장이었는데, 이 오인 때문에 둘로 쪼개지고
        old_text가 양쪽에 전체 문장 그대로 중복 삽입되는 결과를 냈다."""
        text = (
            "행정안전부 예규 「지방자치단체 입찰시 낙찰자 결정기준」 제7장 "
            "제3절의 4.(제안서의 평가)에 따른 분야별 배점한도를 기준으로 한다."
        )
        frags = split_by_item(text)
        assert len(frags) == 1  # 쪼개지지 않아야 함

    def test_real_item_after_section_word_still_splits(self):
        """참조표현 방지 lookbehind가 진짜 호 목록까지 막으면 안 된다 —
        "절"/"장" 등의 단어와 무관한 위치의 정상적인 호 나열은 그대로 쪼개져야."""
        text = "1. 첫째 절차 2. 둘째 절차"
        frags = split_by_item(text)
        assert [f.marker for f in frags] == ["1.", "2."]

    def test_paren_enclosed_reference_number_not_mistaken_for_item(self):
        """★★ 실측(2026-07-18, 정보보호 및 개인정보보호 관리체계 인증 등에
        관한 고시 제23조③2.): "별표 7의2 가목(1.1.2. 항목 제외) 및 나목"
        처럼 괄호 안에 다른 문서(별표)의 세부항목 번호를 가리키는 참조
        표현이 있으면, 그 안의 "1."이 진짜 호 경계로 오인되어 호2가
        괄호 중간에서 잘리고 괄호 안 내용이 가짜 "호 1."로 떨어져
        나갔었다."""
        text = (
            "1. 정보보호 및 개인정보보호 관리체계 인증 : 별표 7의2 가목부터 다목  "
            "2. 정보보호 관리체계 인증 : 별표 7의2 가목(1.1.2. 항목 제외) 및 나목"
        )
        frags = split_by_item(text)
        assert [f.marker for f in frags] == ["1.", "2."]
        assert frags[1].text == "정보보호 관리체계 인증 : 별표 7의2 가목(1.1.2. 항목 제외) 및 나목"

    def test_real_item_immediately_after_closing_paren_still_splits(self):
        """대칭 회귀 방지: 괄호가 닫힌 *바로 다음*에 오는 진짜 호 경계는
        마스킹의 영향을 받지 않고 정상적으로 쪼개져야 한다."""
        text = "1. 내용(참고) 2. 다음 내용"
        frags = split_by_item(text)
        assert [f.marker for f in frags] == ["1.", "2."]


class TestSubItemSplit:
    def test_korean_markers(self):
        text = "가. 첫째 목 나. 둘째 목 다. 셋째 목"
        frags = split_by_subitem(text)
        assert [f.marker for f in frags] == ["가.", "나.", "다."]

    def test_sentence_ending_not_mistaken_for_subitem(self):
        """실측(전자정부법 제56조의2 항①): '포함한다.'의 '다.'가 목 기호
        '다.'와 우연히 겹쳐, 문장 중간이 잘리면 안 된다."""
        text = (
            "중앙사무관장기관의 장은 행정기관등의 장이 정보시스템(정보시스템 "
            "운영시설을 포함한다. 이하 이 조에서 같다) 장애관리를 위한 계획을 "
            "작성하여야 한다."
        )
        assert split_by_subitem(text) == []

    def test_glued_subitems_after_sentence_ending_false_positive(self):
        """실측(전자정부법 제2조제11호): 목 마커가 앞 목 내용에 공백 없이
        바로 붙어있고(정보나.), 그 앞쪽 전제문에는 '말한다.'/'한정한다.'처럼
        가짜 '다.' 후보가 섞여 있어도, 진짜 가~바 목록만 분해되어야 한다."""
        text = (
            "11.  “정보자원”이란 행정기관등이 보유하거나 이용하는 다음 각 목의 "
            "자원을 말한다. 다만, 이용하는 경우에는 나목부터 라목까지에 "
            "한정한다.가. 행정정보나. 정보시스템다. 정보시스템의 구축에 적용되는 "
            "정보기술라. 정보시스템의 운영에 필요한 건축물 및 건축설비(이하 "
            "“정보시스템 운영시설”이라 한다)마. 정보화 예산바. 정보화 인력"
        )
        frags = split_by_subitem(text)
        markers = [f.marker for f in frags]
        # 목록 시작 전 전제문("…한정한다.")은 마커 없는 조각으로 앞에 남는다.
        assert markers == [None, "가.", "나.", "다.", "라.", "마.", "바."]
        assert "정보자원" in frags[0].text
        assert frags[1].text == "행정정보"

    def test_single_stray_marker_not_treated_as_list(self):
        """목 마커처럼 보이는 글자가 문장 안에 달랑 하나뿐이면 목록으로
        보지 않는다 — 최소 2개 이상 증가하는 나열이어야 인정."""
        text = "이 조에서 같다) 이러한 절차를 거쳐야 한다."
        assert split_by_subitem(text) == []


class TestSplitAll:
    def test_hierarchical(self):
        text = "① 항 내용 ② 다음 각 호와 같다 1. 첫째 호 2. 둘째 호"
        frags = split_all(text)
        assert len(frags) >= 3

    def test_empty(self):
        assert split_all("") == []
        assert split_all("   ") == []

    def test_single_item_still_splits_subitems(self):
        """실측(전자정부법 제2조제11호): 블록 안에 호가 정확히 1개뿐이어도
        그 호에 딸린 목 분해가 건너뛰어지면 안 된다. 예전엔 items 개수가
        1개면 '분해 불필요'로 보고 통째로 되돌려, 목 분해가 통째로
        생략되는 버그가 있었다."""
        text = (
            "11. 정보자원이란 다음 각 목의 자원을 말한다.가. 행정정보나. "
            "정보시스템다. 정보기술라. 건축물마. 예산바. 인력"
        )
        frags = split_all(text)
        markers = [f.marker for f in frags]
        assert markers == [None, "가.", "나.", "다.", "라.", "마.", "바."]
        # 전제문("11. 정보자원이란…")도 별도 조각으로 살아있어야 검색 가능
        assert any(f.marker is None and "정보자원" in f.text for f in frags)


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