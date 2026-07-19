"""contract/export.py build_contract 테스트. repo 는 모킹, 실제 조립 로직만 검증."""

from datetime import date
from unittest.mock import MagicMock

from lawtrack.contract.export import build_contract
from lawtrack.db.repo import WatchlistEntry


def _watchlist_repo():
    repo = MagicMock()
    repo.get.return_value = WatchlistEntry(
        law_id="001357", law_type="법률", official_name="공공기관의 정보공개에 관한 법률",
        internal_name="공공기관의 정보공개에 관한 법률", dept_codes=(),
    )
    return repo


def _article_diff_repo():
    repo = MagicMock()
    repo.fetch_period.return_value = [
        {
            "law_id": "001357", "law_serial_no": "251019",
            "article_code": "22", "article_label": "제22조",
            "clause_no": "", "item_label": "", "subitem_label": "",
            "change_type": "개정", "old_text": "구 문장", "new_text": "신 문장",
            "match_status": "성공", "enforce_date": date(2023, 11, 17),
        },
    ]
    return repo


def _change_log_repo():
    repo = MagicMock()
    repo.fetch_latest_for_serial.return_value = {
        "promulgation_no": "", "revision_type": "일부개정",
        "revision_reason": "국무총리 소속에서 행정안전부장관 소속으로 이관하기 위함",
        "old_serial_no": "200001",
        "unchanged_clauses": {"제22조": ["①", "②"]},
    }
    repo.find_by_promulgation.return_value = []
    return repo


class TestRevisionReasonWiredThrough:
    """실측(2026-07-16): api/fulltext.py 가 이미 API 응답에서 뽑아오는
    "제개정이유"(FullTextResult.revision_reason)가 change_log 에 저장도
    안 되고 contract 에도 담기지 않아 항상 빈 문자열로 나갔다 — schema.py
    의 설계 의도("LLM이 추론할 필요 없게 함")를 무력화하고 있었다."""

    def test_revision_reason_appears_in_law_change(self):
        contract = build_contract(
            _watchlist_repo(), _article_diff_repo(), _change_log_repo(),
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        law = contract.amendment_groups[0].laws[0]
        assert law.revision_reason == "국무총리 소속에서 행정안전부장관 소속으로 이관하기 위함"

    def test_old_serial_no_appears_in_law_change(self):
        contract = build_contract(
            _watchlist_repo(), _article_diff_repo(), _change_log_repo(),
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        law = contract.amendment_groups[0].laws[0]
        assert law.old_serial_no == "200001"

    def test_unchanged_clauses_appears_in_law_change(self):
        """★ 실측(2026-07-16): detect.py 가 법제처의 항제개정유형 필드를 읽어
        "이번에 안 바뀐 항"을 change_log.unchanged_clauses 에 이미 저장하지만,
        contract 조립 단계에서 옮겨 담지 않으면 LawChange 에서 항상 빈 dict로
        나가 LLM팀이 "나머지 항도 바뀐 건가?"를 스스로 추론해야 하는 상황이
        재발한다."""
        contract = build_contract(
            _watchlist_repo(), _article_diff_repo(), _change_log_repo(),
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        law = contract.amendment_groups[0].laws[0]
        assert law.unchanged_clauses == {"제22조": ["①", "②"]}

    def test_missing_unchanged_clauses_falls_back_to_empty_dict(self):
        cl_repo = MagicMock()
        cl_repo.fetch_latest_for_serial.return_value = {
            "promulgation_no": "", "revision_type": "", "revision_reason": "",
            "old_serial_no": "",
        }
        cl_repo.find_by_promulgation.return_value = []

        contract = build_contract(
            _watchlist_repo(), _article_diff_repo(), cl_repo,
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        law = contract.amendment_groups[0].laws[0]
        assert law.unchanged_clauses == {}


class TestNoComparisonReporting:
    """★★ 실측 발견(2026-07-18): build_contract()의 amendment_groups 조립은
    article_diff에서 시작해 (law_id, serial_no) 집합을 얻는데, 신구법 대비
    자체가 불가능한 건(제정/폐지제정 등)은 정의상 article_diff 행이 0개라
    그 집합에 원천적으로 들어갈 수 없었다 — NoComparisonItem 스키마는 있었지만
    실제로 채워진 적이 한 번도 없는 죽은 코드였다. change_log를 직접
    조회하는 fetch_no_comparison_in_period로 고쳤다."""

    def test_no_comparison_entry_reported_even_with_zero_article_diff_rows(self):
        article_diff_repo = MagicMock()
        article_diff_repo.fetch_period.return_value = []  # 신구법없음 건은 diff 행이 0개

        cl_repo = MagicMock()
        cl_repo.fetch_no_comparison_in_period.return_value = [
            {"law_id": "037812", "new_serial_no": "2100000268206"},
        ]
        cl_repo.fetch_latest_for_serial.return_value = {
            "revision_type": "폐지제정", "promulgation_no": "2025-116",
        }
        cl_repo.find_by_promulgation.return_value = []

        watchlist_repo = MagicMock()
        watchlist_repo.get.return_value = WatchlistEntry(
            law_id="037812", law_type="행정규칙",
            official_name="중소기업자간 경쟁제품 직접생산 확인기준",
            internal_name="중소기업자간 경쟁제품 직접생산 확인기준", dept_codes=(),
        )

        contract = build_contract(
            watchlist_repo, article_diff_repo, cl_repo,
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        assert len(contract.no_comparison) == 1
        item = contract.no_comparison[0]
        assert item.law_id == "037812"
        assert item.new_serial_no == "2100000268206"
        assert item.reason == "폐지제정"

    def test_no_comparison_entry_not_duplicated_when_already_in_amendment_groups(self):
        """(law_id, serial_no)가 이미 diff 기반 경로로 잡혔다면(구조상 있을 수
        없지만 방어적으로) no_comparison에 중복 보고하지 않는다."""
        article_diff_repo = MagicMock()
        article_diff_repo.fetch_period.return_value = [
            {
                "law_id": "001357", "law_serial_no": "251019",
                "article_code": "22", "article_label": "제22조",
                "clause_no": "", "item_label": "", "subitem_label": "",
                "change_type": "개정", "old_text": "구", "new_text": "신",
                "match_status": "성공", "enforce_date": date(2023, 11, 17),
            },
        ]
        cl_repo = _change_log_repo()
        cl_repo.fetch_no_comparison_in_period.return_value = [
            {"law_id": "001357", "new_serial_no": "251019"},
        ]

        contract = build_contract(
            _watchlist_repo(), article_diff_repo, cl_repo,
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        assert contract.no_comparison == []

    def test_missing_change_log_falls_back_to_empty(self):
        """change_log 에 매칭 행이 없으면(cl=None) 빈 문자열로 안전하게 처리."""
        cl_repo = MagicMock()
        cl_repo.fetch_latest_for_serial.return_value = None
        cl_repo.find_by_promulgation.return_value = []

        contract = build_contract(
            _watchlist_repo(), _article_diff_repo(), cl_repo,
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        law = contract.amendment_groups[0].laws[0]
        assert law.revision_reason == ""
        assert law.old_serial_no == ""
