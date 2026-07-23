"""contract/export.py build_contract 테스트. repo 는 모킹, 실제 조립 로직만 검증."""

from datetime import date
from unittest.mock import MagicMock

from lawtrack.contract.export import build_contract, build_contract_for_versions
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
        "promulgation_no": "20476", "promulgation_date": date(2024, 10, 22),
        "revision_type": "일부개정",
        "revision_reason": "국무총리 소속에서 행정안전부장관 소속으로 이관하기 위함",
        "old_serial_no": "200001",
        "unchanged_clauses": {"제22조": ["①", "②"]},
    }
    repo.find_by_promulgation.return_value = []
    return repo


class TestDetectedVersionReporting:
    """주간 배치는 시행일이 아니라 이번 실행에서 감지한 버전을 보고한다."""

    def test_detected_version_is_included_even_when_enforce_date_is_old(self):
        article_repo = MagicMock()
        old_diff = _article_diff_repo().fetch_period.return_value[0]
        article_repo.fetch_versions.return_value = [old_diff]

        contract = build_contract_for_versions(
            _watchlist_repo(), article_repo, _change_log_repo(),
            versions={("001357", "251019")},
            from_date=date(2026, 7, 15),
            to_date=date(2026, 7, 22),
            batch_date=date(2026, 7, 22),
        )

        assert contract.total_law_count == 1
        law = contract.amendment_groups[0].laws[0]
        assert law.new_serial_no == "251019"
        assert law.enforce_date == "2023-11-17"
        assert contract.period.from_date == "2026-07-15"
        assert contract.period.to_date == "2026-07-22"
        group = contract.amendment_groups[0]
        assert group.promulgation_no == "20476"
        assert group.promulgation_date == "2024-10-22"
        assert group.revision_type == "일부개정"
        article_repo.fetch_versions.assert_called_once_with({("001357", "251019")})
        article_repo.fetch_period.assert_not_called()

    def test_only_versions_detected_in_current_run_are_requested(self):
        article_repo = MagicMock()
        article_repo.fetch_versions.return_value = []

        contract = build_contract_for_versions(
            _watchlist_repo(), article_repo, _change_log_repo(),
            versions=set(),
            from_date=date(2026, 7, 15),
            to_date=date(2026, 7, 22),
        )

        assert contract.total_law_count == 0
        assert contract.no_comparison == []
        article_repo.fetch_versions.assert_called_once_with(set())

    def test_detected_no_comparison_version_is_reported(self):
        article_repo = MagicMock()
        article_repo.fetch_versions.return_value = []
        cl_repo = _change_log_repo()
        cl_repo.fetch_latest_for_serial.return_value = {
            "revision_type": "폐지제정",
            "promulgation_no": "",
        }

        contract = build_contract_for_versions(
            _watchlist_repo(), article_repo, cl_repo,
            versions={("001357", "251019")},
            no_comparison_versions={("001357", "251019")},
            from_date=date(2026, 7, 15),
            to_date=date(2026, 7, 22),
        )

        assert len(contract.no_comparison) == 1
        assert contract.no_comparison[0].reason == "폐지제정"


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


class TestStructuralExpansionGrouping:
    """★ 설계(2026-07-19, LLM팀 산출물 리뷰): match_status="구조확장(구법미분리)"
    행들(구법엔 없던 호/목 구조가 신법에서 새로 생겨 old_text가 여러 행에
    복제되는 케이스)은 articles[]가 아니라 structural_expansions[]로 완전히
    분리되어야 한다 — articles[]는 항상 1:1만 담는다는 전제를 지키기 위함."""

    def _diff_repo_with_expansion(self):
        repo = MagicMock()
        repo.fetch_period.return_value = [
            {
                "law_id": "009199", "law_serial_no": "268103",
                "article_code": "2", "article_label": "제2조",
                "clause_no": "", "item_label": "11.", "subitem_label": "",
                "change_type": "개정", "old_text": "11. 정보자원이란...",
                "new_text": "정보자원이란 다음 각 목과 같다.",
                "match_status": "구조확장(구법미분리)", "enforce_date": date(2023, 11, 17),
            },
            {
                "law_id": "009199", "law_serial_no": "268103",
                "article_code": "2", "article_label": "제2조",
                "clause_no": "", "item_label": "11.", "subitem_label": "가.",
                "change_type": "개정", "old_text": "11. 정보자원이란...",
                "new_text": "가. 행정정보",
                "match_status": "구조확장(구법미분리)", "enforce_date": date(2023, 11, 17),
            },
            {
                "law_id": "009199", "law_serial_no": "268103",
                "article_code": "2", "article_label": "제2조",
                "clause_no": "", "item_label": "11.", "subitem_label": "나.",
                "change_type": "개정", "old_text": "11. 정보자원이란...",
                "new_text": "나. 정보시스템",
                "match_status": "구조확장(구법미분리)", "enforce_date": date(2023, 11, 17),
            },
            # 진짜 1:1 케이스도 하나 섞어서 articles[]엔 이것만 남는지 확인
            {
                "law_id": "009199", "law_serial_no": "268103",
                "article_code": "3", "article_label": "제3조",
                "clause_no": "①", "item_label": "", "subitem_label": "",
                "change_type": "개정", "old_text": "구", "new_text": "신",
                "match_status": "성공", "enforce_date": date(2023, 11, 17),
            },
        ]
        return repo

    def _watchlist_repo_009199(self):
        repo = MagicMock()
        repo.get.return_value = WatchlistEntry(
            law_id="009199", law_type="법률", official_name="전자정부법",
            internal_name="전자정부법", dept_codes=(),
        )
        return repo

    def test_expansion_rows_excluded_from_articles(self):
        contract = build_contract(
            self._watchlist_repo_009199(), self._diff_repo_with_expansion(), _change_log_repo(),
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        law = contract.amendment_groups[0].laws[0]
        assert len(law.articles) == 1
        assert law.articles[0].article_label == "제3조"

    def test_expansion_rows_grouped_into_one_entry_with_shared_old_text(self):
        contract = build_contract(
            self._watchlist_repo_009199(), self._diff_repo_with_expansion(), _change_log_repo(),
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        law = contract.amendment_groups[0].laws[0]
        assert len(law.structural_expansions) == 1
        exp = law.structural_expansions[0]
        assert exp.article_label == "제2조"
        assert exp.old_text == "11. 정보자원이란..."
        assert len(exp.new_items) == 3
        assert [it.subitem_label for it in exp.new_items] == ["", "가.", "나."]
        assert exp.new_items[1].text == "가. 행정정보"

    def test_expansion_across_different_clauses_grouped_together(self):
        """★★ 실측(2026-07-19, 전자정부법 제56조의3①~④): 항 구분조차 없던
        조문 하나가 통째로 새 항(①②③④) 여러 개로 재작성되면, old_text는
        4행 전부 동일한데 clause_no는 행마다 다르다(①,②,③,④). 그룹 키에
        clause_no가 섞여 있으면 이 4행이 서로 다른 "1개짜리 그룹" 4개로
        쪼개진다 — 정의상 구조확장은 1:N이어야 하므로 이건 모순이다."""
        repo = MagicMock()
        repo.fetch_period.return_value = [
            {
                "law_id": "009199", "law_serial_no": "268103",
                "article_code": "56003", "article_label": "제56조의3",
                "clause_no": clause, "item_label": "", "subitem_label": "",
                "change_type": "개정", "old_text": "행정안전부장관은 국가비상사태...",
                "new_text": f"{clause} 새 항 내용 {clause}",
                "match_status": "구조확장(구법미분리)", "enforce_date": date(2023, 11, 17),
            }
            for clause in ("①", "②", "③", "④")
        ]
        contract = build_contract(
            self._watchlist_repo_009199(), repo, _change_log_repo(),
            from_date=date(2020, 1, 1), to_date=date(2100, 1, 1),
        )
        law = contract.amendment_groups[0].laws[0]
        assert law.articles == []
        assert len(law.structural_expansions) == 1  # 4개가 아니라 1개 그룹
        exp = law.structural_expansions[0]
        assert len(exp.new_items) == 4
        assert [it.clause_no for it in exp.new_items] == ["①", "②", "③", "④"]
