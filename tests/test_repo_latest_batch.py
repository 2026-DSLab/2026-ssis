"""LawSummaryRepo.latest_batch_date() — 웹페이지가 "이번 배치"를 찾는 진입점.

실제 MySQL 없이, cursor()가 돌려주는 값만으로 SQL이 기대한 대로 쓰이는지
확인한다(다른 law_summary 테스트와 같은 가짜 DB 패턴 — tests/test_summary_db.py 참고).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

from lawtrack.db.repo import LawSummaryRepo


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._row


class _FakeDb:
    def __init__(self, row):
        self._cursor = _FakeCursor(row)

    @contextmanager
    def cursor(self, *, dictionary: bool = True):
        yield None, self._cursor


def test_latest_batch_date_returns_max_date():
    db = _FakeDb({"d": date(2026, 7, 20)})
    repo = LawSummaryRepo(db)

    assert repo.latest_batch_date() == date(2026, 7, 20)


def test_latest_batch_date_none_when_table_empty():
    """law_summary 가 비어 있으면 MAX()는 NULL이 담긴 행 하나를 돌려준다
    (0행이 아니다) — None으로 정확히 변환되는지 확인한다."""
    db = _FakeDb({"d": None})
    repo = LawSummaryRepo(db)

    assert repo.latest_batch_date() is None
