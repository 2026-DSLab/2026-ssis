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
    """실측: dept_name 이 entry.dept_codes 와 무관하게 항상
    None으로 하드코딩돼 있어, watchlist.dept_codes 를 채워도 동명이인
    행정규칙(예: 재정경제부판 vs 타부처판 "협상에 의한 계약체결기준")을
    구분하지 못하고 영구히 AMBIGUOUS 로 막히는 버그가 있었음."""

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
    골라냄. 실측((계약예규) 정부 입찰ㆍ계약 집행기준 제34조):
    한 조문 안에서 일부 항만 바뀌고 나머지는 그대로인 경우가 흔함 —
    이걸 명시적으로 알려주면 요약 단계가 "나머지 항도 바뀐 건가?"를 추론할
    필요가 없어짐."""

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
        """실측(전자정부법 제2조): 항(①②③) 없이 호가 조문에
        바로 붙는 조문은 라벨 없는 더미 ClauseNode(no="", change_type="")
        하나로 표현됨. 이 더미는 실제 항이 아닌데 change_type이 비어있다는
        이유로 "안 바뀐 항"에 포함되면 {"제2조": [""]} 처럼 의미 없는 빈
        문자열 라벨이 그대로 노출됐음."""
        dummy_clause = ClauseNode(no="", text="", change_type="", change_dates="")
        art = ArticleUnit(
            code="2", branch="", label="제2조", title=None, changed=True,
            clauses=(dummy_clause,),
        )
        located = [(None, [_success(_unit("제2조", ""))])]
        assert _unchanged_clauses([art], located) == {}

    def test_article_with_no_clause_level_tagging_reports_nothing(self):
        """실측(청소년복지 지원법 제16조의2): 조문 자체가
        신설/재구성된 경우 법제처 API가 그 조문 안 "모든" 항의
        항제개정유형을 통째로 비워둠(None) — 일부만 개정된 조문과
        달리 "바뀐 항에만 값 채움" 규칙이 아예 적용되지 않음. 이걸
        구분 안 하면 방금 change_type="신설"로 저장한 항이 같은 조문
        안에서 동시에 "현행유지"로도 보고되는 자기모순이 생겼음
        ({"제16조의2": ["①","②"]}가 신설 항목과 충돌). 항 전부의
        change_type이 비어있으면 이 조문에 대해서는 아무것도 확정하지
        않아야 함."""
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
        기존대로 정상 동작해야 함 — 하나라도 change_type이 채워져
        있으면 나머지 빈 항은 진짜 현행유지로 신뢰함."""
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


class TestAlreadyProcessedSignal:
    """실측 버그(개인정보 보호법 시행령 011468): 개정 여부를 documents 존재
    여부로만 판정하던 시절, 전문 비교 화면이 과거 버전을 documents 에 캐시로
    넣어 두면 배치가 그 버전을 "이미 처리함"으로 오해해 현행 교체를 통째로
    놓쳤음. 판정 기준을 watchlist.last_serial_no 로 옮긴 뒤의 회귀 테스트다.
    """

    def test_web_cache_does_not_mask_a_new_current_version(self):
        """documents 에 있어도, watchlist 가 다른 번호면 개정으로 잡아야 함."""
        entry = _entry(last_serial_no="286175")
        version_repo = MagicMock()
        version_repo.admrul_exists.return_value = True   # 열람 캐시로 이미 들어와 있음

        with patch("lawtrack.detect.resolve_admrul") as mock_resolve:
            mock_resolve.return_value = ResolveOutcome("matched", [MagicMock(serial_no="283503")])
            result = detect_admrul(MagicMock(), version_repo, entry)

        assert result.status is DetectStatus.CHANGED
        assert result.current_serial_no == "283503"

    def test_same_serial_with_fulltext_is_unchanged(self):
        """정상 상태 — 번호도 같고 전문도 갖고 있으면 할 일이 없음."""
        entry = _entry(last_serial_no="283503")
        version_repo = MagicMock()
        version_repo.admrul_exists.return_value = True

        with patch("lawtrack.detect.resolve_admrul") as mock_resolve:
            mock_resolve.return_value = ResolveOutcome("matched", [MagicMock(serial_no="283503")])
            result = detect_admrul(MagicMock(), version_repo, entry)

        assert result.status is DetectStatus.UNCHANGED

    def test_smaller_serial_still_counts_as_change(self):
        """번호가 줄어드는 교체도 개정임 — 시행 유예분이 나중에 현행이 되면
        현행본의 일련번호가 직전보다 작을 수 있음."""
        entry = _entry(last_serial_no="286175")
        version_repo = MagicMock()
        version_repo.admrul_exists.return_value = False

        with patch("lawtrack.detect.resolve_admrul") as mock_resolve:
            mock_resolve.return_value = ResolveOutcome("matched", [MagicMock(serial_no="100")])
            result = detect_admrul(MagicMock(), version_repo, entry)

        assert result.status is DetectStatus.CHANGED

    def test_first_backfill_falls_back_to_documents(self):
        """last_serial_no 가 비어 있는 최초 백필에서는 documents 로 판단."""
        entry = _entry(last_serial_no=None)
        version_repo = MagicMock()
        version_repo.admrul_exists.return_value = True

        with patch("lawtrack.detect.resolve_admrul") as mock_resolve:
            mock_resolve.return_value = ResolveOutcome("matched", [MagicMock(serial_no="777")])
            result = detect_admrul(MagicMock(), version_repo, entry)

        assert result.status is DetectStatus.UNCHANGED

    def test_seeded_serial_without_fulltext_still_backfills(self):
        """갓 구축한 DB — scripts/load_watchlist.py 가 last_serial_no 를 미리
        채워 두므로 번호는 이미 현행과 같음. 그래도 전문이 없으면 받아와야 함.

        번호만 보고 판정하게 만들었더니 최초 백필이 통째로 건너뛰어졌던 회귀를
        막음 — documents 가 비어 있으면 전문을 받아야 화면도 비교도 성립함.
        """
        entry = _entry(last_serial_no="777")          # seed 가 넣어 둔 값
        version_repo = MagicMock()
        version_repo.admrul_exists.return_value = False  # 전문은 아직 없음

        with patch("lawtrack.detect.resolve_admrul") as mock_resolve:
            mock_resolve.return_value = ResolveOutcome("matched", [MagicMock(serial_no="777")])
            result = detect_admrul(MagicMock(), version_repo, entry)

        assert result.status is DetectStatus.CHANGED
