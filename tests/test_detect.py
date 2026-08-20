"""detect.py 회귀 테스트. resolve_law/resolve_admrul 을 모킹해 순수 분기 로직만 검증."""

from unittest.mock import MagicMock, patch

from lawtrack.api.search import ResolveOutcome
from lawtrack.db.repo import WatchlistEntry
from lawtrack.detect import DetectStatus, _unchanged_clauses, detect_admrul
from lawtrack.locate.locator import LocateResult, LocateStatus
from lawtrack.parse.fulltext import ArticleUnit, ClauseNode
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
