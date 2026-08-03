"""LawSummaryRepo.fetch_by_period() — "기간 지정 즉석 조회" 기능(webapp/live.py)이
쓰는 조회. 실제 PostgreSQL 없이, cursor()에 넘어가는 SQL/파라미터와 디코딩만
확인한다(다른 law_summary 테스트와 같은 가짜 DB 패턴)."""

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


def test_fetch_by_period_filters_by_created_at_between():
    """★ 설계(2026-08-03, 사용자 결정): enforce_date(실제 법 시행일)가
    아니라 created_at(우리가 처음 감지/처리한 날)로 거른다 — "이번 주"
    배치(batch_date 기준)와 같은 개념으로 통일해, "이번 주엔 69건인데
    최근 1개월엔 1건"처럼 서로 다른 기준이 섞여 숫자가 안 맞는 걸 막는다."""
    db = _FakeDb([])
    LawSummaryRepo(db).fetch_by_period("2026-07-01", "2026-07-20")

    sql, params = db._cursor.calls[0]
    assert "created_at::date BETWEEN" in sql
    assert params == (date(2026, 7, 1), date(2026, 7, 20))


def test_fetch_by_period_decodes_json_columns():
    row = {
        "law_id": "009199", "caveats": '["확인 필요"]',
        "article_summaries": None, "mappings": "[]", "verifier_issues": None,
    }
    db = _FakeDb([row])
    got = LawSummaryRepo(db).fetch_by_period(date(2026, 7, 1), date(2026, 7, 20))

    assert len(got) == 1
    assert got[0]["caveats"] == ["확인 필요"]
    assert got[0]["article_summaries"] == []  # NULL은 빈 목록으로


def test_fetch_by_period_empty_result():
    db = _FakeDb([])
    got = LawSummaryRepo(db).fetch_by_period(date(2026, 1, 1), date(2026, 1, 2))
    assert got == []
