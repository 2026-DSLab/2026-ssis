"""normalize 회귀 테스트.

모든 케이스는 실제 API 응답 또는 팀 전수검증에서 나온 실패 사례다.
"""

import unicodedata as ud

from lawtrack.text.normalize import (
    DOT_MAP,
    names_match,
    normalize_chars,
    normalize_name,
    normalize_text,
    count_occurrences,
)


class TestUnicodeDots:
    """가운뎃점 5종 — NFC/NFKC 로 해결 안 되는 것이 전제."""

    def test_all_five_dots_are_distinct_codepoints(self):
        dots = ["\u318D", "\u00B7", "\u2027", "\u30FB", "\u2024"]
        assert len(set(dots)) == 5

    def test_nfc_does_not_unify(self):
        assert ud.normalize("NFC", "\u318D") != ud.normalize("NFC", "\u00B7")

    def test_nfkc_does_not_unify_either(self):
        # NFKC 는 NFC 보다 공격적인 호환 정규화인데도 통일되지 않는다.
        assert ud.normalize("NFKC", "\u318D") != ud.normalize("NFKC", "\u00B7")

    def test_custom_map_unifies_all(self):
        for src in DOT_MAP:
            assert normalize_chars(src) == "\u318D"

    def test_one_dot_leader_included(self):
        """U+2024 는 팀 문서 목록에 없던 것. 전자정부법 '관리적․기술적' 실측."""
        assert normalize_chars("관리적\u2024기술적") == "관리적\u318D기술적"


class TestRealLawNames:
    """실측 법령명 매칭 실패 사례."""

    def test_middle_dot_variants(self):
        # 표 표기: 초중등교육법 / 실제: 초ㆍ중등교육법
        assert names_match("초\u00B7중등교육법", "초\u318D중등교육법")
        assert names_match("저출산\u00B7고령사회기본법", "저출산\u318D고령사회기본법")
        assert names_match("대\u00B7중소기업 상생협력", "대\u318D중소기업 상생협력")

    def test_spacing_variants(self):
        assert names_match("노인일자리 및 사회활동", "노인 일자리 및 사회활동")
        assert names_match("한국장학재단설립 등에", "한국장학재단 설립 등에")

    def test_prefix_removal(self):
        assert names_match("예정가격작성기준", "(계약예규) 예정가격 작성기준")

    def test_suffix_law_to_law_full(self):
        assert names_match("산림복지 진흥에 관한 법", "산림복지 진흥에 관한 법률")
        assert names_match("장애인활동 지원에 관한 법", "장애인활동 지원에 관한 법률")
        assert names_match("고독사 예방 및 관리에 관한 법", "고독사 예방 및 관리에 관한 법률")

    def test_similar_law_must_not_match(self):
        """유사법 오염 방지 — 완전일치라 다른 법은 걸러져야 한다."""
        assert not names_match(
            "국가를 당사자로 하는 계약에 관한 법률",
            "지방자치단체를 당사자로 하는 계약에 관한 법률",
        )
        assert not names_match("보조금 관리에 관한 법률", "지방자치단체 보조금 관리에 관한 법률")
        assert not names_match("전기사업법", "전기공사업법")

    def test_renamed_law_does_not_match(self):
        """제명 변경은 정규화로 해결 불가 — 매핑 테이블 필요함을 확인."""
        assert not names_match("소프트웨어산업 진흥법", "소프트웨어 진흥법")
        assert not names_match("국가정보화 기본법", "지능정보화 기본법")

    def test_abbreviation_does_not_match(self):
        """약칭도 정규화로 해결 불가 — 법령약칭명 필드 필요."""
        assert not names_match("사회보장급여법", "사회보장급여의 이용ㆍ제공 및 수급권자 발굴에 관한 법률")


class TestArticleText:
    """조문 검색용."""

    def test_single_space_difference(self):
        a = "개인정보의 안전성 확보에 필요한 조치"
        b = "개인정보의  안전성 확보에 필요한조치"
        assert normalize_text(a) == normalize_text(b)

    def test_tilde_operator(self):
        # 실측: "③ ∼ ⑨ (생 략)" — U+223C 사용
        assert normalize_text("③ \u223C ⑨") == normalize_text("③~⑨")

    def test_duplicate_detection(self):
        haystack = "통계청이 공표하는 자료. 또한 통계청이 정한다."
        assert count_occurrences(haystack, "통계청이") == 2