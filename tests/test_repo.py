"""ArticleDiffRepo._to_row 테스트. DB 연결 없이 순수 로직만 검증."""

from datetime import date

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
    """실측(전자정부법, 2026-07-16): 위치확정 실패/삭제 건은 unit이 없어
    article_code 등이 전부 빈 문자열이 되고, UNIQUE KEY가 그 빈 값들로만
    구성돼 있어 같은 법령·버전에서 실패가 2건 이상이면 서로 덮어써
    마지막 1건만 남았다. change.index + frag_idx로 고유화되어야 한다."""

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
        여러 개면 fragment(frag_idx)로도 구분되어야 한다."""
        change = _change(index=3)
        row_a = ArticleDiffRepo._to_row(
            "X", "1", change, _unresolved_result(), date(2025, 1, 1), 0,
        )
        row_b = ArticleDiffRepo._to_row(
            "X", "1", change, _unresolved_result(), date(2025, 1, 1), 1,
        )
        assert row_a[2] != row_b[2]

    def test_resolved_unit_still_uses_unit_fields(self):
        """정상 매칭(unit 있음)은 예전처럼 unit 필드를 그대로 쓴다 (회귀 방지)."""
        unit = SearchUnit(
            article_code="56", article_label="제56조의2", clause_no="①",
            item_label="", subitem_label="", text="본문", changed=True,
        )
        result = LocateResult(LocateStatus.SUCCESS, unit, None, 1, ())
        row = ArticleDiffRepo._to_row("009199", "268103", _change(0), result, date(2025, 1, 1), 0)
        assert row[2] == "56"
        assert row[3] == "제56조의2"
        assert row[4] == "①"


class TestToRowFragmentSpecificText:
    """실측(2026-07-16, contract/export.py 실데이터 검증 — 전자정부법 제56조의2
    항①~⑤): 한 change가 여러 조각(항①②③…)으로 쪼개지면, 그동안 모든 조각의
    new_text 에 change.new_clean(쪼개지기 전 원본 통짜 블록)을 그대로 저장해,
    서로 다른 위치의 행들이 전부 똑같은 거대한 원문 덩어리를 갖는 버그가
    있었다. new_text 는 실제 위치확정된 조각(fragment)의 텍스트여야 한다."""

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
    """★★ 실측(2026-07-16, (계약예규) 예정가격작성기준 제40조② 등): new_text 는
    locate/locator.py 가 검색 직전에 strip_annotations 를 적용한 텍스트로
    조각을 만들어 각주가 안 섞이지만, old_text 는 change.old_clean 을 그대로
    써서 "<img id="...">" "<개정 2014.1.10.>" 같은 태그가 LLM팀에게 그대로
    노출되고 있었다."""

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
        마커 텍스트를 그대로 노출할 필요가 없다 — 빈 문자열이 더 명확하다."""
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
    """★★ 실측(2026-07-16, (계약예규) 정부 입찰ㆍ계약 집행기준 제34조): 항이
    여러 개 신설되어 뒤의 항 번호가 밀리면, 법제처 신구조문대비표 원본이
    "구법 N번째 항"과 "신법 N번째 항"을 내용이 아니라 순서로만 짝지어
    제공한다 — 구③(기존 지급기한 규정)과 신③(완전히 새로운 규정)이 마치
    같은 조항의 개정 전/후인 것처럼 match_status=성공으로 나갔었다. 같은
    조문 안에 순수 신설(NEWLY_CREATED) 항목이 있으면 그 조문의 '개정' 행은
    '위치재배치의심'으로 표시해 LLM팀이 old_text를 그대로 신뢰하지 않게
    한다."""

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

    def test_flagged_old_text_carries_inline_warning_prefix(self):
        """★ 실측(2026-07-19, LLM팀 산출물 리뷰): match_status 필드를 따로
        안 보고 old_text만 훑어도 "이거 못 믿는다"가 바로 보이게, old_text
        앞에 "[※...]" 안내문을 붙인다 — 법령 원문과 섞이지 않는 형식."""
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
        old_text = row[9]
        assert old_text.startswith("[※")
        assert "구" in old_text  # 원래 old_text("구")가 뒤에 그대로 남아있어야 함

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
        """신설이 다른 조문에서 일어났다면, 이 조문의 개정 행은 영향받지 않는다."""
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
    """★★ 실측(2026-07-19, 전자정부법 제2조11호 가~바): 신설이 전혀 없는
    순수 '개정'인데도, 구법엔 목(가~바) 구조 자체가 없던 통짜 문단이
    신법에서 목 6개로 쪼개지면 old_text가 6개 행 전부에 똑같이 재사용된다.
    _reshuffled_articles()는 "같은 조문에 신설이 섞였는가"만 보므로 이
    케이스를 놓쳐 match_status=성공으로 잘못 확정된다. "같은 change가
    2곳 이상으로 성공 위치확정됐는가"를 직접 보는 old_text_shared가
    이 틈을 메운다.

    ★ 실측(2026-07-19, LLM팀 산출물 리뷰): 처음엔 이것도 "위치재배치의심"
    으로 표시했는데, 원인이 전혀 다른 reshuffled_articles 케이스(항 신설로
    순서가 밀려 신/구가 잘못 짝지어짐)와 같은 이름을 쓰니 "재배치"라는
    말이 이 케이스엔 안 맞아 헷갈린다는 지적을 받았다 — 여긴 재배치가
    아니라 애초에 구법에 대응하는 조각이 없는 것(구조확장)이다. 값
    이름을 "구조확장(구법미분리)"로 분리했다."""

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
        # ★ 설계(2026-07-19): "구조확장" 케이스는 DB 단계에서 old_text에
        # 안내문을 붙이지 않는다 — contract/export.py가 이 상태를 보고
        # articles[]가 아닌 별도 StructuralExpansion 배열로 빼내므로,
        # 배열 구조 자체가 의미를 전달한다(schema.py 참고). DB의 old_text는
        # 항상 순수 원문 그대로여야 한다.
        assert row[9] == "구"

    def test_single_location_not_flagged(self):
        """change 하나가 정확히 한 곳에만 위치확정되면(1:1), old_text가
        재사용될 여지가 없으니 플래그하면 안 된다(과다플래그 방지)."""
        change = self._amended_change(0)
        row = ArticleDiffRepo._to_row(
            "X", "1", change,
            LocateResult(LocateStatus.SUCCESS, self._unit("제2조", ""), None, 1, ()),
            date(2025, 1, 1), 0, frozenset(), old_text_shared=False,
        )
        assert row[11] == "성공"

    def test_newly_created_not_flagged_by_shared_flag(self):
        """신설 행은 old_text가 항상 빈 문자열이라 '재사용' 개념 자체가
        의미 없다 — old_text_shared가 True로 넘어와도 신설 타입이면
        무시해야 한다(insert_results가 애초에 AMENDED만 True로 넘기지만,
        _to_row 자체도 change_type으로 한 번 더 방어한다)."""
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
        """두 원인이 같은 조문에서 동시에 발생할 수 있다(호/목 구조확장이
        일어난 조문에 다른 항의 신설도 섞인 경우) — 이때는 더 구체적인
        원인(구조확장)을 우선 표시한다. "위치재배치의심"이라고 하면 마치
        순서가 밀린 것처럼 오해하지만, 실제 원인은 구법에 대응 조각
        자체가 없는 것이기 때문이다."""
        change = self._amended_change(0)
        row = ArticleDiffRepo._to_row(
            "X", "1", change,
            LocateResult(LocateStatus.SUCCESS, self._unit("제2조", "11.", "가."), None, 1, ()),
            date(2025, 1, 1), 0, reshuffled_articles={"제2조"}, old_text_shared=True,
        )
        assert row[11] == "구조확장(구법미분리)"
