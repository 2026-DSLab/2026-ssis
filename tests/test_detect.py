"""detect.py 회귀 테스트. resolve_law/resolve_admrul 을 모킹해 순수 분기 로직만 검증."""

from unittest.mock import MagicMock, patch

import json

from lawtrack.api.fulltext import FullTextResult
from lawtrack.api.oldnew import OldNewResult, VersionInfo
from lawtrack.api.search import ResolveOutcome
from lawtrack.db.repo import WatchlistEntry
from lawtrack.detect import (
    DetectStatus,
    _fulltext_identity_error,
    _search_identity_error,
    _serialize_articles,
    _serialize_units,
    _unchanged_clauses,
    detect_admrul,
)
from lawtrack.locate.locator import LocateResult, LocateStatus
from lawtrack.parse.fulltext import ArticleUnit, ClauseNode, ItemNode, SearchUnit, SubItemNode
from lawtrack.text.split import Fragment, Level


def _entry(**overrides) -> WatchlistEntry:
    base = dict(law_id="27946", law_type="행정규칙", official_name="협상에 의한 계약체결기준")
    base.update(overrides)
    return WatchlistEntry(**base)


class TestDetectAdmrulDeptName:
    """실측(2026-07-16): dept_name 이 entry.dept_codes 와 무관하게 항상
    None으로 하드코딩돼 있어, watchlist.dept_codes 를 채워도 동명이인
    행정규칙(예: 재정경제부판 vs 타부처판 "협상에 의한 계약체결기준")을
    구분하지 못하고 영구히 AMBIGUOUS 로 막히는 버그가 있었다."""

    def test_passes_entry_dept_codes_as_dept_name(self):
        entry = _entry(dept_codes=("재정경제부",))
        version_repo = MagicMock()
        version_repo.admrul_exists.return_value = False

        with patch("lawtrack.detect.resolve_admrul") as mock_resolve:
            mock_resolve.return_value = ResolveOutcome("matched", [MagicMock(serial_no="123")])
            detect_admrul(MagicMock(), version_repo, entry)

        _, kwargs = mock_resolve.call_args
        assert kwargs["dept_name"] == "재정경제부"

    def test_no_dept_codes_passes_none(self):
        """dept_codes 가 비어있으면 예전처럼 None (부처 조건 없이 검색)."""
        entry = _entry(dept_codes=())
        version_repo = MagicMock()
        version_repo.admrul_exists.return_value = False

        with patch("lawtrack.detect.resolve_admrul") as mock_resolve:
            mock_resolve.return_value = ResolveOutcome("matched", [MagicMock(serial_no="123")])
            detect_admrul(MagicMock(), version_repo, entry)

        _, kwargs = mock_resolve.call_args
        assert kwargs["dept_name"] is None

    def test_ambiguous_result_propagates(self):
        entry = _entry(dept_codes=())
        version_repo = MagicMock()

        with patch("lawtrack.detect.resolve_admrul") as mock_resolve:
            mock_resolve.return_value = ResolveOutcome("ambiguous", [MagicMock(), MagicMock()])
            result = detect_admrul(MagicMock(), version_repo, entry)

        assert result.status is DetectStatus.AMBIGUOUS


class TestFetchedLawIdentity:
    """이름 검색 뒤 다른 ID/본문이 섞여도 DB 저장 전에 차단한다."""

    def test_leading_zero_law_ids_are_the_same_identity(self):
        entry = _entry(
            law_id="001357",
            law_type="법률",
            official_name="공공기관의 정보공개에 관한 법률",
        )
        assert _search_identity_error(
            entry,
            source_id="1357",
            source_name="공공기관의 정보공개에 관한 법률",
        ) == ""

    def test_different_source_id_is_rejected(self):
        entry = _entry()
        error = _search_identity_error(
            entry,
            source_id="99999",
            source_name=entry.official_name,
        )
        assert "API 응답 ID 99999" in error

    def test_oldnew_serial_must_match_fulltext_serial(self):
        entry = _entry()
        fulltext = FullTextResult(
            raw={},
            serial_no="200",
            source_id="27946",
            name=entry.official_name,
            revision_reason="",
            revision_text="",
        )
        current = VersionInfo(
            serial_no="201",
            source_id="27946",
            name=entry.official_name,
            enforce_date="",
            promulgation_date="",
            promulgation_no="",
            revision_type="",
            is_current=True,
        )
        oldnew = OldNewResult(
            available=True,
            reason="",
            old_version=VersionInfo("", "", "", "", "", "", "", False),
            new_version=current,
        )

        error = _fulltext_identity_error(entry, fulltext, oldnew=oldnew)

        assert "신법 일련번호 201" in error


def _unit(article_label, clause_no):
    from lawtrack.parse.fulltext import SearchUnit

    return SearchUnit(
        article_code=article_label, article_label=article_label,
        clause_no=clause_no, item_label="", subitem_label="", text="x", changed=True,
    )


def _success(unit):
    return LocateResult(status=LocateStatus.SUCCESS, unit=unit, fragment=None, match_count=1)


class TestUnchangedClauses:
    """법령 전용: 개정된 조문 중 항제개정유형이 빈 값인(=현행 유지) 항만
    골라낸다. 실측(2026-07-16, (계약예규) 정부 입찰ㆍ계약 집행기준 제34조):
    한 조문 안에서 일부 항만 바뀌고 나머지는 그대로인 경우가 흔하다 —
    이걸 명시적으로 알려주면 LLM팀이 "나머지 항도 바뀐 건가?"를 추론할
    필요가 없어진다."""

    def _article(self):
        clauses = (
            ClauseNode(no="①", text="1항", change_type="", change_dates=""),
            ClauseNode(no="②", text="2항", change_type="", change_dates=""),
            ClauseNode(no="③", text="3항", change_type="개정", change_dates="2026.1.1."),
        )
        return ArticleUnit(
            code="34", branch="", label="제34조", title=None, changed=True, clauses=clauses,
        )

    def test_unchanged_labels_collected_for_touched_article(self):
        articles = [self._article()]
        located = [(None, [_success(_unit("제34조", "③"))])]
        result = _unchanged_clauses(articles, located)
        assert result == {"제34조": ["①", "②"]}

    def test_article_not_touched_is_excluded(self):
        articles = [self._article()]
        located = [(None, [_success(_unit("제99조", "①"))])]
        assert _unchanged_clauses(articles, located) == {}

    def test_no_successful_matches_returns_empty(self):
        articles = [self._article()]
        located = [(None, [LocateResult(
            status=LocateStatus.ZERO_MATCH, unit=None, fragment=None, match_count=0,
        )])]
        assert _unchanged_clauses(articles, located) == {}

    def test_article_without_clauses_excluded(self):
        art = ArticleUnit(
            code="1", branch="", label="제1조", title="목적", changed=True, clauses=(),
        )
        located = [(None, [_success(_unit("제1조", ""))])]
        assert _unchanged_clauses([art], located) == {}

    def test_headerless_dummy_clause_not_reported_as_unchanged(self):
        """★★ 실측(2026-07-16, 전자정부법 제2조): 항(①②③) 없이 호가 조문에
        바로 붙는 조문은 라벨 없는 더미 ClauseNode(no="", change_type="")
        하나로 표현된다. 이 더미는 실제 항이 아닌데 change_type이 비어있다는
        이유로 "안 바뀐 항"에 포함되면 {"제2조": [""]} 처럼 의미 없는 빈
        문자열 라벨이 그대로 노출됐다."""
        dummy_clause = ClauseNode(no="", text="", change_type="", change_dates="")
        art = ArticleUnit(
            code="2", branch="", label="제2조", title=None, changed=True,
            clauses=(dummy_clause,),
        )
        located = [(None, [_success(_unit("제2조", ""))])]
        assert _unchanged_clauses([art], located) == {}

    def test_article_with_no_clause_level_tagging_reports_nothing(self):
        """★★★ 실측(2026-07-18, 청소년복지 지원법 제16조의2): 조문 자체가
        신설/재구성된 경우 법제처 API가 그 조문 안 "모든" 항의
        항제개정유형을 통째로 비워둔다(None) — 일부만 개정된 조문과
        달리 "바뀐 항에만 값 채움" 규칙이 아예 적용되지 않는다. 이걸
        구분 안 하면 방금 change_type="신설"로 저장한 항이 같은 조문
        안에서 동시에 "현행유지"로도 보고되는 자기모순이 생겼다
        ({"제16조의2": ["①","②"]}가 신설 항목과 충돌). 항 전부의
        change_type이 비어있으면 이 조문에 대해서는 아무것도 확정하지
        않아야 한다."""
        clauses = (
            ClauseNode(no="①", text="1항", change_type="", change_dates=""),
            ClauseNode(no="②", text="2항", change_type="", change_dates=""),
        )
        art = ArticleUnit(
            code="16", branch="2", label="제16조의2", title=None, changed=True, clauses=clauses,
        )
        located = [(None, [_success(_unit("제16조의2", "①"))])]
        assert _unchanged_clauses([art], located) == {}

    def test_article_with_partial_tagging_still_reports_unchanged(self):
        """대칭 회귀 방지: 일부 항만 개정된 조문(제31조의2, 제75조 유형)은
        기존대로 정상 동작해야 한다 — 하나라도 change_type이 채워져
        있으면 나머지 빈 항은 진짜 현행유지로 신뢰한다."""
        clauses = (
            ClauseNode(no="①", text="1항", change_type="", change_dates=""),
            ClauseNode(no="②", text="2항", change_type="", change_dates=""),
            ClauseNode(no="③", text="3항", change_type="개정", change_dates="2026.1.1."),
        )
        art = ArticleUnit(
            code="75", branch="", label="제75조", title=None, changed=True, clauses=clauses,
        )
        located = [(None, [_success(_unit("제75조", "③"))])]
        assert _unchanged_clauses([art], located) == {"제75조": ["①", "②"]}


class TestParsedStructureSerialization:
    """★ 실측 요구사항(2026-07-18): law_full_text(원본)와 별도로 조/항/호/목
    파싱 결과도 DB에 저장해야 한다 — dataclasses.asdict() 로 만든 결과가
    실제로 JSON 직렬화 가능하고, 중첩 구조(항→호→목)가 그대로 보존되는지
    확인한다."""

    def test_article_tree_round_trips_through_json(self):
        subitem = SubItemNode(label="가.", text="목 내용")
        item = ItemNode(no="1.", branch="", text="호 내용", subitems=(subitem,))
        clause = ClauseNode(no="①", text="항 내용", change_type="개정", change_dates="2026.1.1.", items=(item,))
        art = ArticleUnit(
            code="1", branch="", label="제1조", title="목적", changed=True,
            content="제1조(목적) 항 내용", enforce_date="20260101", clauses=(clause,),
        )
        serialized = _serialize_articles([art])
        # JSON 직렬화가 실제로 되어야 함 (dataclass 등 비-JSON 타입이 안 섞여야 함)
        dumped = json.loads(json.dumps(serialized, ensure_ascii=False))
        assert dumped[0]["label"] == "제1조"
        assert dumped[0]["clauses"][0]["no"] == "①"
        assert dumped[0]["clauses"][0]["items"][0]["subitems"][0]["label"] == "가."

    def test_search_units_round_trip_through_json(self):
        unit = SearchUnit(
            article_code="1", article_label="제1조", clause_no="①",
            item_label="", subitem_label="", text="본문", changed=True,
        )
        serialized = _serialize_units([unit])
        dumped = json.loads(json.dumps(serialized, ensure_ascii=False))
        assert dumped[0]["article_label"] == "제1조"
        assert dumped[0]["text"] == "본문"

    def test_annotation_tags_stripped_from_stored_article_text(self):
        """★★ 실측 발견(2026-07-18, 전자정부법 제5조③): parse_articles()가
        만드는 ClauseNode.text 등은 lawService 원문 그대로라 "<개정
        2020.6.9>" 같은 각주가 안 지워진 채 그대로 law_articles_parsed에
        저장되고 있었다. article_diff.old_text에서 이미 한 번 고친
        문제(strip_annotations 누락)가 새 캐시 컬럼에서 재발한 것 —
        직렬화 단계에서 지워야 한다."""
        clause = ClauseNode(
            no="③",
            text="③ 전자정부기본계획을 고려하여야 한다. <개정 2020.6.9>",
            change_type="", change_dates="",
        )
        art = ArticleUnit(
            code="5", branch="", label="제5조", title=None, changed=True,
            content="제5조 <개정 2013.3.23>", clauses=(clause,),
        )
        serialized = _serialize_articles([art])
        assert "<개정" not in serialized[0]["content"]
        assert "<개정" not in serialized[0]["clauses"][0]["text"]
        assert "전자정부기본계획을 고려하여야 한다." in serialized[0]["clauses"][0]["text"]

    def test_annotation_tags_stripped_from_stored_admrul_units(self):
        unit = SearchUnit(
            article_code="1", article_label="제1조", clause_no="",
            item_label="", subitem_label="", text="본문 <img id=\"1\"></img> 내용", changed=True,
        )
        serialized = _serialize_units([unit])
        assert "<img" not in serialized[0]["text"]
        assert "</img>" not in serialized[0]["text"]

    def test_label_and_marker_fields_untouched_by_stripping(self):
        """text/content가 아닌 필드(라벨, 마커, 날짜 등)는 그대로 유지되어야
        한다 — 과도하게 넓게 지우는 회귀 방지."""
        clause = ClauseNode(no="①", text="내용", change_type="개정", change_dates="<개정 2020.1.1>")
        art = ArticleUnit(code="1", branch="", label="제1조", title=None, changed=True, clauses=(clause,))
        serialized = _serialize_articles([art])
        # change_dates는 text/content 키가 아니므로 안 건드려야 함
        assert serialized[0]["clauses"][0]["change_dates"] == "<개정 2020.1.1>"
