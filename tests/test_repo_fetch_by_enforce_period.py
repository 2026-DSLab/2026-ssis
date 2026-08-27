"""LawSummaryRepo.fetch_by_enforce_period() — 주간 리포트(지난 달력 주) 화면이
쓰는 조회. 실제 PostgreSQL 없이, cursor()에 넘어가는 SQL/파라미터와 디코딩만
확인함(다른 law_summary 테스트와 같은 가짜 DB 패턴)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

from lawtrack.db.repo import LawSummaryRepo


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self._cursor = _FakeCursor(rows)

    @contextmanager
    def cursor(self, *, dictionary: bool = True):
        yield None, self._cursor


def test_filters_by_enforce_date_between():
    db = _FakeDb([])
    LawSummaryRepo(db).fetch_by_enforce_period("2026-08-17", "2026-08-23")

    sql, params = db._cursor.calls[0]
    assert "enforce_date BETWEEN" in sql
    assert params == (date(2026, 8, 17), date(2026, 8, 23))


def test_does_not_filter_by_batch_date_or_created_at():
    """배치가 만든 행이든 기간 즉석 조회가 만든 행이든(batch_date NULL)
    시행일만 창에 들면 함께 걸려야 함 — 한 창을 두 경로가 나눠 요약해도
    화면에는 하나로 모여야 하기 때문.

    created_at(감지일)도 쓰면 안 됨: 감지일은 우리 쪽 사정이라 시행이
    한참 지난 개정도 "최근"으로 만들어 버림(실측: 최근 1개월 탭에
    시행일 217일 전인 지능정보화 기본법이 들어왔음)."""
    db = _FakeDb([])
    LawSummaryRepo(db).fetch_by_enforce_period(date(2026, 8, 17), date(2026, 8, 23))

    sql, _ = db._cursor.calls[0]
    assert "batch_date" not in sql
    assert "created_at" not in sql


def test_decodes_json_columns():
    row = {
        "law_id": "009199", "caveats": '["확인 필요"]',
        "article_summaries": None, "mappings": "[]", "verifier_issues": None,
    }
    db = _FakeDb([row])
    got = LawSummaryRepo(db).fetch_by_enforce_period(date(2026, 8, 17), date(2026, 8, 23))

    assert len(got) == 1
    assert got[0]["caveats"] == ["확인 필요"]
    assert got[0]["article_summaries"] == []  # NULL은 빈 목록으로


def test_empty_result():
    db = _FakeDb([])
    got = LawSummaryRepo(db).fetch_by_enforce_period(date(2026, 1, 1), date(2026, 1, 7))
    assert got == []


def test_no_detection_date_window_query_remains():
    """감지일(created_at)로 기간을 자르는 조회가 아예 없어야 함.

    예전에 fetch_by_period(created_at 기준)가 있었고, "최근 N일" 탭이
    그걸 썼음 — 그래서 시행이 창 밖인 개정이 화면에 딸려 들어왔음
    (실측: 최근 5일에 시행일 7일 전, 최근 1개월에 시행일 217일 전).
    선택지 자체를 없애야 같은 실수가 다시 안 들어옴. created_at 컬럼
    자체는 "언제 처음 봤나"를 남기는 기록으로 계속 씀 — 금지하는 건
    그걸로 화면 창을 자르는 것뿐임.
    """
    import inspect

    src = inspect.getsource(LawSummaryRepo)

    assert "created_at::date BETWEEN" not in src
