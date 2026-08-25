"""ArticleDiffRepo._to_row / insert_results 테스트. DB 연결 없이 순수 로직만 검증."""

from datetime import date
from unittest.mock import MagicMock, patch

from lawtrack.db.repo import ArticleDiffRepo
from lawtrack.locate.locator import LocateResult, LocateStatus
from lawtrack.parse.fulltext import SearchUnit
from lawtrack.parse.oldnew import ArticleChange, ChangeType
from lawtrack.text.split import Fragment, Level


def _change(index: int) -> ArticleChange:
    return ArticleChange(
        index=index,
        change_type=ChangeType.AMENDED,
        old_raw="<P>구</P>", new_raw="<P>신</P>",
        old_clean="구", new_clean="신",
    )


def _unresolved_result() -> LocateResult:
    return LocateResult(LocateStatus.ZERO_MATCH, None, None, 0, ("0건 미발견",))


class TestToRowUnresolvedUniqueness:
    """실측(전자정부법): 위치확정 실패/삭제 건은 unit이 없어
    article_code 등이 전부 빈 문자열이 되고, UNIQUE KEY가 그 빈 값들로만
    구성돼 있어 같은 법령·버전에서 실패가 2건 이상이면 서로 덮어써
    마지막 1건만 남았음. change.index + frag_idx로 고유화되어야 함."""

    def test_two_failures_in_different_changes_get_distinct_keys(self):
        row_a = ArticleDiffRepo._to_row(
            "009199", "268103", _change(index=2), _unresolved_result(), date(2025, 1, 1), 0,
        )
        row_b = ArticleDiffRepo._to_row(
            "009199", "268103", _change(index=7), _unresolved_result(), date(2025, 1, 1), 0,
        )
        key_a = row_a[2:7]  # article_code, article_label, clause_no, item_label, subitem_label
        key_b = row_b[2:7]
        assert key_a != key_b
        assert row_a[2] != ""  # article_code no longer blank

    def test_two_failures_in_same_change_get_distinct_keys(self):
        """국방데이터·인공지능업무 훈령 사례처럼 한 change 안에 <P> 가
        여러 개면 fragment(frag_idx)로도 구분되어야 함."""
        change = _change(index=3)
        row_a = ArticleDiffRepo._to_row(
            "X", "1", change, _unresolved_result(), date(2025, 1, 1), 0,
        )
        row_b = ArticleDiffRepo._to_row(
            "X", "1", change, _unresolved_result(), date(2025, 1, 1), 1,
        )
        assert row_a[2] != row_b[2]

    def test_resolved_unit_still_uses_unit_fields(self):
        """정상 매칭(unit 있음)은 예전처럼 unit 필드를 그대로 씀 (회귀 방지)."""
        unit = SearchUnit(
            article_code="56", article_label="제56조의2", clause_no="①",
            item_label="", subitem_label="", text="본문", changed=True,
        )
        result = LocateResult(LocateStatus.SUCCESS, unit, None, 1, ())
        row = ArticleDiffRepo._to_row("009199", "268103", _change(0), result, date(2025, 1, 1), 0)
        assert row[2] == "56"
        assert row[3] == "제56조의2"
        assert row[4] == "①"


class TestToRowDeletionSkipLabel:
    """실측 발견(지능정보화 기본법 HWPX):
    삭제(DELETED_SKIP)는 unit이 없는 게 "위치를 못 찾아서"가 아니라
    "삭제된 내용은 현재 조문에 없으니 애초에 찾을 필요가 없어서"다 —
    진짜 위치확정 실패(0건실패/중복실패)와 근본 원인이 다른데 같은
    "(위치미상#N-M)" 라벨을 써서 보고서만 보면 정상 처리된 삭제 건이
    실패한 것처럼 보였음."""

    def _deleted_change(
        self, index: int, old_clean: str = "구", article_context: str | None = None,
    ) -> ArticleChange:
        return ArticleChange(
            index=index, change_type=ChangeType.DELETED,
            old_raw="<P>구</P>", new_raw="<P><삭  제></P>",
            old_clean=old_clean, new_clean="<삭  제>",
            article_context=article_context,
        )

    def test_deleted_skip_gets_clear_label_not_위치미상(self):
        row = ArticleDiffRepo._to_row(
            "000028", "1", self._deleted_change(0), _unresolved_result(), date(2025, 1, 1), 0,
        )
        assert "위치미상" not in row[3]
        assert row[3] == "(삭제됨 — 개정 전 조문 참고)"

    def test_deleted_skip_shows_leading_clause_marker_when_present(self):
        """실측(지능정보화 기본법 후속 질문): old_text가 "①
        국가기관등은…"처럼 항 기호로 시작하면, 조 번호는 몰라도 몇 항이었는지는
        재조회 없이 이미 가진 데이터에서 보여줄 수 있어야 함."""
        change = self._deleted_change(0, old_clean="①  국가기관등은 정보통신망을 통하여…")
        row = ArticleDiffRepo._to_row(
            "000028", "1", change, _unresolved_result(), date(2025, 1, 1), 0,
        )
        assert row[3] == "(삭제됨 — 개정 전 ①항 참고)"

    def test_deleted_skip_shows_leading_item_marker_when_present(self):
        change = self._deleted_change(0, old_clean="3. 정보격차의 실태 및 해소 현황")
        row = ArticleDiffRepo._to_row(
            "000028", "1", change, _unresolved_result(), date(2025, 1, 1), 0,
        )
        assert row[3] == "(삭제됨 — 개정 전 3호 참고)"

    def test_deleted_skip_shows_leading_subitem_marker_when_present(self):
        change = self._deleted_change(0, old_clean="가. 세부 기준")
        row = ArticleDiffRepo._to_row(
            "000028", "1", change, _unresolved_result(), date(2025, 1, 1), 0,
        )
        assert row[3] == "(삭제됨 — 개정 전 가목 참고)"

    def test_deleted_skip_shows_article_context_with_marker(self):
        """실측(지능정보화 기본법 후속 질문 — "몇 조였는지도
        보여달라"): extract_changes()가 채운 article_context(조 번호)와
        old_text 선행 기호(항/호/목)를 합쳐 "제46조①항"처럼 완전한 위치를
        보여줘야 함."""
        change = self._deleted_change(
            0, old_clean="①  국가기관등은 정보통신망을 통하여…", article_context="제46조",
        )
        row = ArticleDiffRepo._to_row(
            "000028", "1", change, _unresolved_result(), date(2025, 1, 1), 0,
        )
        assert row[3] == "(삭제됨 — 개정 전 제46조①항 참고)"

    def test_deleted_skip_shows_article_context_without_marker(self):
        """old_text 자체엔 기호가 없어도(호/항 마커 없는 통짜 문장) 조
        번호만이라도 알면 그것만 보여줌 — 부분 정보라도 정직하게."""
        change = self._deleted_change(
            0, old_clean="정보통신접근성 품질인증의 유효기간은 1년으로 한다.",
            article_context="제48조",
        )
        row = ArticleDiffRepo._to_row(
            "000028", "1", change, _unresolved_result(), date(2025, 1, 1), 0,
        )
        assert row[3] == "(삭제됨 — 개정 전 제48조 참고)"

    def test_deleted_skip_falls_back_when_no_leading_marker(self):
        """기호 없이 평문으로 시작하고 조 번호도 못 찾으면(예: 헤더 없는
        통짜 조문) 기존처럼 조문 참고 안내만 남김 — 없는 정보를 지어내지
        않음."""
        change = self._deleted_change(0, old_clean="과학기술정보통신부장관은 …에 관한 사항을 정한다.")
        row = ArticleDiffRepo._to_row(
            "000028", "1", change, _unresolved_result(), date(2025, 1, 1), 0,
        )
        assert row[3] == "(삭제됨 — 개정 전 조문 참고)"

    def test_deleted_skip_article_code_still_unique(self):
        """라벨(사람이 보는 값)만 바뀌었을 뿐, article_code(DB 유일성
        목적)는 여전히 change.index/frag_idx로 고유해야 함."""
        row_a = ArticleDiffRepo._to_row(
            "000028", "1", self._deleted_change(0), _unresolved_result(), date(2025, 1, 1), 0,
        )
        row_b = ArticleDiffRepo._to_row(
            "000028", "1", self._deleted_change(1), _unresolved_result(), date(2025, 1, 1), 0,
        )
        assert row_a[2] != row_b[2]

    def test_genuine_location_failure_still_gets_위치미상_label(self):
        """진짜 위치확정 실패(AMENDED인데 unit 없음)는 여전히 "위치미상"
        라벨을 써야 함 — 삭제와 헷갈리면 안 되는 건 반대 방향도 마찬가지."""
        row = ArticleDiffRepo._to_row(
            "000028", "1", _change(0), _unresolved_result(), date(2025, 1, 1), 0,
        )
        assert "위치미상" in row[3]


class TestToRowFragmentSpecificText:
    """실측(contract/export.py 실데이터 검증 — 전자정부법 제56조의2
    항①~⑤): 한 change가 여러 조각(항①②③…)으로 쪼개지면, 그동안 모든 조각의
    new_text 에 change.new_clean(쪼개지기 전 원본 통짜 블록)을 그대로 저장해,
    서로 다른 위치의 행들이 전부 똑같은 거대한 원문 덩어리를 갖는 버그가
    있었음. new_text 는 실제 위치확정된 조각(fragment)의 텍스트여야 함."""

    def _change_with_multi_fragment_new_clean(self) -> ArticleChange:
        return ArticleChange(
            index=0, change_type=ChangeType.AMENDED,
            old_raw="<P>구</P>", new_raw="<P>① 첫째 ② 둘째</P>",
            old_clean="제1조(제목) 구", new_clean="제1조(제목) ① 첫째 ② 둘째",
        )

    def test_new_text_uses_fragment_not_whole_change_block(self):
        change = self._change_with_multi_fragment_new_clean()
        unit = SearchUnit(
            article_code="1", article_label="제1조", clause_no="①",
            item_label="", subitem_label="", text="① 첫째", changed=True,
        )
        frag = Fragment(Level.CLAUSE, "①", "첫째", "① 첫째")
        result = LocateResult(LocateStatus.SUCCESS, unit, frag, 1, ())

        row = ArticleDiffRepo._to_row("X", "1", change, result, date(2025, 1, 1), 0)
        new_text = row[10]

        assert new_text == "① 첫째"
        assert new_text != change.new_clean  # 통짜 블록 전체가 아니어야 함

    def test_different_fragments_of_same_change_get_different_new_text(self):
        change = self._change_with_multi_fragment_new_clean()
        unit1 = SearchUnit(
            article_code="1", article_label="제1조", clause_no="①",
            item_label="", subitem_label="", text="① 첫째", changed=True,
        )
        unit2 = SearchUnit(
            article_code="1", article_label="제1조", clause_no="②",
            item_label="", subitem_label="", text="② 둘째", changed=True,
        )
        frag1 = Fragment(Level.CLAUSE, "①", "첫째", "① 첫째")
        frag2 = Fragment(Level.CLAUSE, "②", "둘째", "② 둘째")
        result1 = LocateResult(LocateStatus.SUCCESS, unit1, frag1, 1, ())
        result2 = LocateResult(LocateStatus.SUCCESS, unit2, frag2, 1, ())

        row1 = ArticleDiffRepo._to_row("X", "1", change, result1, date(2025, 1, 1), 0)
        row2 = ArticleDiffRepo._to_row("X", "1", change, result2, date(2025, 1, 1), 1)

        assert row1[10] != row2[10]  # new_text 가 서로 달라야 함 (실측 버그: 둘 다 동일했음)


class TestOldTextAnnotationStripping:
    """실측((계약예규) 예정가격작성기준 제40조② 등): new_text 는
    locate/locator.py 가 검색 직전에 strip_annotations 를 적용한 텍스트로
    조각을 만들어 각주가 안 섞이지만, old_text 는 change.old_clean 을 그대로
    써서 "<img id="...">" "<개정 2014.1.10.>" 같은 태그가 요약 단계에 그대로
    노출되고 있었음."""

    def test_img_tag_stripped_from_old_text(self):
        change = ArticleChange(
            index=0, change_type=ChangeType.AMENDED,
            old_raw="<P>구</P>", new_raw="<P>신</P>",
            old_clean='②일반관리비는 다음과 같다.<img id="123"></img>', new_clean="신",
        )
        unit = SearchUnit(
            article_code="40", article_label="제40조", clause_no="②",
            item_label="", subitem_label="", text="x", changed=True,
        )
        result = LocateResult(LocateStatus.SUCCESS, unit, None, 1, ())
        row = ArticleDiffRepo._to_row("X", "1", change, result, date(2025, 1, 1), 0)
        old_text = row[9]
        assert "<img" not in old_text
        assert "</img>" not in old_text

    def test_revision_date_annotation_stripped_from_old_text(self):
        change = ArticleChange(
            index=0, change_type=ChangeType.AMENDED,
            old_raw="<P>구</P>", new_raw="<P>신</P>",
            old_clean="1. 특수기술이 필요한 공사 <개정 2014.1.10.>", new_clean="신",
        )
        unit = SearchUnit(
            article_code="4", article_label="제4조", clause_no="",
            item_label="1.", subitem_label="", text="x", changed=True,
        )
        result = LocateResult(LocateStatus.SUCCESS, unit, None, 1, ())
        row = ArticleDiffRepo._to_row("X", "1", change, result, date(2025, 1, 1), 0)
        old_text = row[9]
        assert "<개정" not in old_text
        assert "특수기술이 필요한 공사" in old_text

    def test_newly_created_marker_becomes_empty_not_literal_tag(self):
        """change_type 이 이미 "신설"을 명시하므로, old_text 에 "<신  설>"
        마커 텍스트를 그대로 노출할 필요가 없음 — 빈 문자열이 더 명확함."""
        change = ArticleChange(
            index=0, change_type=ChangeType.NEWLY_CREATED,
            old_raw="<P><신  설></P>", new_raw="<P>새 항 내용</P>",
            old_clean="<신  설>", new_clean="새 항 내용",
        )
        unit = SearchUnit(
            article_code="5", article_label="제5조", clause_no="③",
            item_label="", subitem_label="", text="x", changed=True,
        )
        result = LocateResult(LocateStatus.SUCCESS, unit, None, 1, ())
        row = ArticleDiffRepo._to_row("X", "1", change, result, date(2025, 1, 1), 0)
        assert row[9] == ""


class TestReshuffledArticleFlagging:
    """실측((계약예규) 정부 입찰ㆍ계약 집행기준 제34조): 항이
    여러 개 신설되어 뒤의 항 번호가 밀리면, 법제처 신구조문대비표 원본이
    "구법 N번째 항"과 "신법 N번째 항"을 내용이 아니라 순서로만 짝지어
    제공함 — 구③(기존 지급기한 규정)과 신③(완전히 새로운 규정)이 마치
    같은 조항의 개정 전/후인 것처럼 match_status=성공으로 나갔었음. 같은
    조문 안에 순수 신설(NEWLY_CREATED) 항목이 있으면 그 조문의 '개정' 행은
    '위치재배치의심'으로 표시해 요약 단계가 old_text를 그대로 신뢰하지 않게
    함."""

    def _amended_change(self, index: int) -> ArticleChange:
        return ArticleChange(
            index=index, change_type=ChangeType.AMENDED,
            old_raw="<P>구</P>", new_raw="<P>신</P>", old_clean="구", new_clean="신",
        )

    def _newly_created_change(self, index: int) -> ArticleChange:
        return ArticleChange(
            index=index, change_type=ChangeType.NEWLY_CREATED,
            old_raw="<P><신  설></P>", new_raw="<P>새 항 내용</P>",
            old_clean="<신  설>", new_clean="새 항 내용",
        )

    def _unit(self, article_label: str, clause_no: str) -> SearchUnit:
        return SearchUnit(
            article_code=article_label, article_label=article_label, clause_no=clause_no,
            item_label="", subitem_label="", text="x", changed=True,
        )

    def test_amended_row_flagged_when_sibling_clause_newly_created(self):
        amended = self._amended_change(0)
        created = self._newly_created_change(1)
        results = [
            (amended, [LocateResult(LocateStatus.SUCCESS, self._unit("제34조", "③"), None, 1, ())]),
            (created, [LocateResult(LocateStatus.SUCCESS, self._unit("제34조", "⑬"), None, 1, ())]),
        ]
        reshuffled = ArticleDiffRepo._reshuffled_articles(results)
        assert reshuffled == {"제34조"}

        row = ArticleDiffRepo._to_row(
            "34470", "1", amended, results[0][1][0], date(2025, 1, 1), 0, reshuffled,
        )
        assert row[11] == "위치재배치의심"  # match_status

    def test_flagged_old_text_stays_pure_original_text(self):
        """실측: match_status="위치재배치의심"이어도 old_text
        앞에 안내문을 덧붙이지 않음 — DB/산출물의 old_text는 항상 순수
        원문 그대로여야 하고, 신뢰도 표시는 match_status 필드만으로 함."""
        amended = self._amended_change(0)
        created = self._newly_created_change(1)
        results = [
            (amended, [LocateResult(LocateStatus.SUCCESS, self._unit("제34조", "③"), None, 1, ())]),
            (created, [LocateResult(LocateStatus.SUCCESS, self._unit("제34조", "⑬"), None, 1, ())]),
        ]
        reshuffled = ArticleDiffRepo._reshuffled_articles(results)
        row = ArticleDiffRepo._to_row(
            "34470", "1", amended, results[0][1][0], date(2025, 1, 1), 0, reshuffled,
        )
        assert row[9] == "구"

    def test_amended_row_not_flagged_without_sibling_newly_created(self):
        amended = self._amended_change(0)
        results = [
            (amended, [LocateResult(LocateStatus.SUCCESS, self._unit("제1조", ""), None, 1, ())]),
        ]
        reshuffled = ArticleDiffRepo._reshuffled_articles(results)
        assert reshuffled == set()

        row = ArticleDiffRepo._to_row(
            "X", "1", amended, results[0][1][0], date(2025, 1, 1), 0, reshuffled,
        )
        assert row[11] == "성공"

    def test_other_article_not_flagged_by_unrelated_newly_created(self):
        """신설이 다른 조문에서 일어났다면, 이 조문의 개정 행은 영향받지 않음."""
        amended = self._amended_change(0)
        created = self._newly_created_change(1)
        results = [
            (amended, [LocateResult(LocateStatus.SUCCESS, self._unit("제5조", "①"), None, 1, ())]),
            (created, [LocateResult(LocateStatus.SUCCESS, self._unit("제34조", "⑬"), None, 1, ())]),
        ]
        reshuffled = ArticleDiffRepo._reshuffled_articles(results)
        row = ArticleDiffRepo._to_row(
            "X", "1", amended, results[0][1][0], date(2025, 1, 1), 0, reshuffled,
        )
        assert row[11] == "성공"


class TestOldTextSharedFlagging:
    """실측(전자정부법 제2조11호 가~바): 신설이 전혀 없는
    순수 '개정'인데도, 구법엔 목(가~바) 구조 자체가 없던 통짜 문단이
    신법에서 목 6개로 쪼개지면 old_text가 6개 행 전부에 똑같이 재사용됨.
    _reshuffled_articles()는 "같은 조문에 신설이 섞였는가"만 보므로 이
    케이스를 놓쳐 match_status=성공으로 잘못 확정됨. "같은 change가
    2곳 이상으로 성공 위치확정됐는가"를 직접 보는 old_text_shared가
    이 틈을 메움.

    실측: 처음엔 이것도 "위치재배치의심"
    으로 표시했는데, 원인이 전혀 다른 reshuffled_articles 케이스(항 신설로
    순서가 밀려 신/구가 잘못 짝지어짐)와 같은 이름을 쓰니 "재배치"라는
    말이 이 케이스엔 안 맞아 헷갈린다는 지적을 받았음 — 여긴 재배치가
    아니라 애초에 구법에 대응하는 조각이 없는 것(구조확장)임. 값
    이름을 "구조확장(구법미분리)"로 분리했음."""

    def _amended_change(self, index: int) -> ArticleChange:
        return ArticleChange(
            index=index, change_type=ChangeType.AMENDED,
            old_raw="<P>구</P>", new_raw="<P>신</P>", old_clean="구", new_clean="신",
        )

    def _unit(self, article_label: str, item_label: str, subitem_label: str = "") -> SearchUnit:
        return SearchUnit(
            article_code=article_label, article_label=article_label, clause_no="",
            item_label=item_label, subitem_label=subitem_label, text="x", changed=True,
        )

    def test_single_change_split_into_multiple_locations_flagged(self):
        change = self._amended_change(0)
        row = ArticleDiffRepo._to_row(
            "X", "1", change,
            LocateResult(LocateStatus.SUCCESS, self._unit("제2조", "11.", "가."), None, 1, ()),
            date(2025, 1, 1), 0, frozenset(), old_text_shared=True,
        )
        assert row[11] == "구조확장(구법미분리)"
        # 설계: "구조확장" 케이스는 DB 단계에서 old_text에
        # 안내문을 붙이지 않음 — contract/export.py가 이 상태를 보고
        # articles[]가 아닌 별도 StructuralExpansion 배열로 빼내므로,
        # 배열 구조 자체가 의미를 전달함(schema.py 참고). DB의 old_text는
        # 항상 순수 원문 그대로여야 함.
        assert row[9] == "구"

    def test_single_location_not_flagged(self):
        """change 하나가 정확히 한 곳에만 위치확정되면(1:1), old_text가
        재사용될 여지가 없으니 플래그하면 안 됨(과다플래그 방지)."""
        change = self._amended_change(0)
        row = ArticleDiffRepo._to_row(
            "X", "1", change,
            LocateResult(LocateStatus.SUCCESS, self._unit("제2조", ""), None, 1, ()),
            date(2025, 1, 1), 0, frozenset(), old_text_shared=False,
        )
        assert row[11] == "성공"

    def test_newly_created_not_flagged_by_shared_flag(self):
        """신설 행은 old_text가 항상 빈 문자열이라 '재사용' 개념 자체가
        의미 없음 — old_text_shared가 True로 넘어와도 신설 타입이면
        무시해야 함(insert_results가 애초에 AMENDED만 True로 넘기지만,
        _to_row 자체도 change_type으로 한 번 더 방어함)."""
        change = ArticleChange(
            index=0, change_type=ChangeType.NEWLY_CREATED,
            old_raw="<P><신  설></P>", new_raw="<P>새 항 내용</P>",
            old_clean="<신  설>", new_clean="새 항 내용",
        )
        row = ArticleDiffRepo._to_row(
            "X", "1", change,
            LocateResult(LocateStatus.SUCCESS, self._unit("제2조", "가."), None, 1, ()),
            date(2025, 1, 1), 0, frozenset(), old_text_shared=True,
        )
        assert row[11] == "성공"

    def test_old_text_shared_takes_precedence_over_reshuffled_label(self):
        """두 원인이 같은 조문에서 동시에 발생할 수 있음(호/목 구조확장이
        일어난 조문에 다른 항의 신설도 섞인 경우) — 이때는 더 구체적인
        원인(구조확장)을 우선 표시함. "위치재배치의심"이라고 하면 마치
        순서가 밀린 것처럼 오해하지만, 실제 원인은 구법에 대응 조각
        자체가 없는 것이기 때문임."""
        change = self._amended_change(0)
        row = ArticleDiffRepo._to_row(
            "X", "1", change,
            LocateResult(LocateStatus.SUCCESS, self._unit("제2조", "11.", "가."), None, 1, ()),
            date(2025, 1, 1), 0, reshuffled_articles={"제2조"}, old_text_shared=True,
        )
        assert row[11] == "구조확장(구법미분리)"


class TestFragmentSpecificOldText:
    """설계: "구조확장이 아닌데도 old_text가 통짜로 재사용되는"
    사례를 줄이기 위해, old_clean도 호 단위까지 쪼개서 이 change의 성공
    위치 전부와 애매함 없이 1:1로 맞출 수 있으면 위치별 정밀 old_text를
    줌. 맞지 않으면(old에 호 구조가 없거나 일부만 맞으면) 기존 통짜
    재사용 + 구조확장 판정으로 그대로 폴백함(all-or-nothing)."""

    def _amended_change(self, old_clean: str, new_clean: str = "신", index: int = 0) -> ArticleChange:
        return ArticleChange(
            index=index, change_type=ChangeType.AMENDED,
            old_raw=f"<P>{old_clean}</P>", new_raw=f"<P>{new_clean}</P>",
            old_clean=old_clean, new_clean=new_clean,
        )

    def _unit(self, item_label: str, clause_no: str = "") -> SearchUnit:
        return SearchUnit(
            article_code="제5조", article_label="제5조", clause_no=clause_no,
            item_label=item_label, subitem_label="", text="x", changed=True,
        )

    def test_clean_item_split_gives_precise_old_text_not_shared(self):
        """구법에도 호 마커가 있어 신법 위치와 1:1로 맞아떨어지면, old_text는
        전체 블록이 아니라 그 호만 받고 match_status는 정상 성공이어야
        함(구조확장으로 과잉분류되면 안 됨)."""
        change = self._amended_change("1. 가 2. 나")
        locate_results = [
            LocateResult(LocateStatus.SUCCESS, self._unit("1."), None, 1, ()),
            LocateResult(LocateStatus.SUCCESS, self._unit("2."), None, 1, ()),
        ]
        lookup = ArticleDiffRepo._fragment_old_text_by_item(change, locate_results)
        assert lookup == {("", "1."): "1. 가", ("", "2."): "2. 나"}

        row0 = ArticleDiffRepo._to_row(
            "X", "1", change, locate_results[0], date(2025, 1, 1), 0,
            frozenset(), old_text_shared=False, fragment_old_lookup=lookup,
        )
        row1 = ArticleDiffRepo._to_row(
            "X", "1", change, locate_results[1], date(2025, 1, 1), 1,
            frozenset(), old_text_shared=False, fragment_old_lookup=lookup,
        )
        assert row0[9] == "1. 가"
        assert row1[9] == "2. 나"
        assert row0[11] == "성공"
        assert row1[11] == "성공"

    def test_old_without_item_markers_falls_back_to_none(self):
        """구법이 호 구조 없이 통짜 문장이면(구조확장 케이스) 정밀 매칭이
        원천적으로 불가능하니 None을 돌려줘 기존 동작으로 폴백해야 함."""
        change = self._amended_change("통짜 구법 문장")
        locate_results = [
            LocateResult(LocateStatus.SUCCESS, self._unit("1."), None, 1, ()),
            LocateResult(LocateStatus.SUCCESS, self._unit("2."), None, 1, ()),
        ]
        assert ArticleDiffRepo._fragment_old_text_by_item(change, locate_results) is None

    def test_partial_match_falls_back_to_none(self):
        """신법 위치 중 하나라도 old쪽에 대응하는 호가 없으면(예: 신설로
        번호가 하나 더 늘어난 경우) 전체를 폴백시킴 — 일부만 정밀
        매칭되는 애매한 상태는 만들지 않음."""
        change = self._amended_change("1. 가 2. 나")
        locate_results = [
            LocateResult(LocateStatus.SUCCESS, self._unit("1."), None, 1, ()),
            LocateResult(LocateStatus.SUCCESS, self._unit("2."), None, 1, ()),
            LocateResult(LocateStatus.SUCCESS, self._unit("3."), None, 1, ()),
        ]
        assert ArticleDiffRepo._fragment_old_text_by_item(change, locate_results) is None

    def test_multiple_new_locations_sharing_same_item_key_falls_back_to_none(self):
        """실측 회귀(전자정부법 제2조11호 가~바): 신법 쪽
        여러 위치(목 가.나.다...)가 (항,호)까지만 보면 전부 같은 키로
        겹칠 수 있음 — old_lookup엔 그 키가 존재는 하므로(중복 없이 1개)
        존재확인만으로는 이걸 못 걸러내 목 6개가 전부 같은 old_text를
        공유한 채 match_status=성공으로 새어나가는 회귀가 있었음. 신법
        쪽 키가 서로 겹치면 반드시 폴백해야 함."""
        change = self._amended_change("11. 통짜 구법 문장")
        locate_results = [
            LocateResult(LocateStatus.SUCCESS, self._unit("11.", clause_no=""), None, 1, ()),
            LocateResult(LocateStatus.SUCCESS, self._unit("11.", clause_no=""), None, 1, ()),
        ]
        assert ArticleDiffRepo._fragment_old_text_by_item(change, locate_results) is None

    def test_single_success_returns_none(self):
        """성공 위치가 1개뿐이면 애초에 old_text 재사용 문제 자체가 없으니
        정밀 매칭을 시도할 필요가 없음(기존 통짜 동작 그대로가 이미
        정확함)."""
        change = self._amended_change("1. 가 2. 나")
        locate_results = [LocateResult(LocateStatus.SUCCESS, self._unit("1."), None, 1, ())]
        assert ArticleDiffRepo._fragment_old_text_by_item(change, locate_results) is None


class TestInsertResultsDedupBeforeExecuteValues:
    """실측 발견((계약예규)
    정부 입찰ㆍ계약 집행기준 34470 실측): results에 change.index가 같은
    ArticleChange가 두 번 섞여 들어오면(위치확정 실패 건의 합성키
    __unresolved__{index}_{frag_idx}가 충돌). 한 INSERT 문 안에서 같은 행을
    마지막 값으로 조용히 덮어썼지만, Postgres의 execute_values(한 INSERT
    문에 여러 행)는 "ON CONFLICT DO UPDATE command cannot affect row a
    second time"로 배치 전체를 실패시킨다. insert_results()가 execute_values를
    부르기 전에 UNIQUE KEY 기준으로 미리 걸러(마지막 값 유지) 이 충돌을
    막는지 확인함."""

    def _change(self, index: int) -> ArticleChange:
        return ArticleChange(
            index=index, change_type=ChangeType.AMENDED,
            old_raw="<P>구</P>", new_raw="<P>신</P>", old_clean="구", new_clean="신",
        )

    def _unresolved(self) -> LocateResult:
        return LocateResult(LocateStatus.ZERO_MATCH, None, None, 0, ("0건 미발견",))

    def test_duplicate_change_index_collapses_to_last_value(self):
        """change.index가 같은(=합성 article_code가 같은) 실패 건 2개가
        results에 섞여 들어와도, execute_values에는 마지막 값 1건만
        전달되어야 함."""
        db = MagicMock()
        cur = MagicMock()
        db.transaction.return_value.__enter__.return_value = (None, cur)
        repo = ArticleDiffRepo(db)

        change_a = self._change(index=5)
        change_b = self._change(index=5)  # 같은 index — 실제로 재현된 실측 케이스
        results = [
            (change_a, [self._unresolved()]),
            (change_b, [self._unresolved()]),
        ]

        with patch("lawtrack.db.repo.execute_values") as mock_execute_values:
            repo.insert_results("34470", "1", results, default_enforce_date=date(2025, 1, 1))

        assert mock_execute_values.called
        rows_passed = mock_execute_values.call_args[0][2]
        assert len(rows_passed) == 1  # 2건 -> 충돌 제거 후 1건
        assert rows_passed[0][2] == "__unresolved__5_0"  # 마지막(change_b) 값이 남음

    def test_no_collision_keeps_all_distinct_rows(self):
        """키가 서로 다르면(정상 케이스) 아무것도 걸러지지 않아야 함."""
        db = MagicMock()
        cur = MagicMock()
        db.transaction.return_value.__enter__.return_value = (None, cur)
        repo = ArticleDiffRepo(db)

        results = [
            (self._change(index=1), [self._unresolved()]),
            (self._change(index=2), [self._unresolved()]),
            (self._change(index=3), [self._unresolved()]),
        ]

        with patch("lawtrack.db.repo.execute_values") as mock_execute_values:
            repo.insert_results("34470", "1", results, default_enforce_date=date(2025, 1, 1))

        rows_passed = mock_execute_values.call_args[0][2]
        assert len(rows_passed) == 3
