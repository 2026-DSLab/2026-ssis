"""lawtrack.week.last_full_week() — 주간 리포트 화면과 주간 배치가 함께 보는
달력 주 경계.

이 창이 흔들리면 배치가 담은 것과 화면이 고르는 것이 어긋나므로, 요일마다
같은 답이 나오는지(주 안에서는 창이 고정인지)를 못박아 둔다.
"""

from __future__ import annotations

from datetime import date, timedelta

from lawtrack.week import last_full_week


def test_returns_previous_monday_to_sunday():
    """2026-08-27 은 목요일 — 지난 달력 주는 08-17(월)~08-23(일)."""
    assert last_full_week(date(2026, 8, 27)) == (date(2026, 8, 17), date(2026, 8, 23))


def test_monday_looks_at_the_week_that_just_ended():
    """월요일 06:00 배치가 도는 시점 — 방금 끝난 주가 대상이어야 함.
    "이번 주가 시작됐으니 이번 주를 보여준다"면 월요일 아침마다 화면이
    비게 됨."""
    assert last_full_week(date(2026, 8, 24)) == (date(2026, 8, 17), date(2026, 8, 23))


def test_sunday_still_looks_at_the_previous_week():
    """일요일은 아직 이번 주가 안 끝난 시점 — 창은 그대로 지난주."""
    assert last_full_week(date(2026, 8, 30)) == (date(2026, 8, 17), date(2026, 8, 23))


def test_window_is_stable_across_the_whole_week():
    """같은 주 안에서는 어느 날 열어도 같은 창 — 볼 때마다 건수가
    달라지던 예전 동작(마지막 배치 기준)을 막는 성질임."""
    windows = {last_full_week(date(2026, 8, 24) + timedelta(days=n)) for n in range(7)}

    assert len(windows) == 1


def test_window_is_exactly_seven_days():
    start, end = last_full_week(date(2026, 8, 27))

    assert (end - start).days == 6
    assert start.weekday() == 0  # 월요일
    assert end.weekday() == 6    # 일요일


def test_crosses_year_boundary():
    """2027-01-01 은 금요일 — 지난 달력 주는 해를 넘어 2026-12-21~12-27."""
    assert last_full_week(date(2027, 1, 1)) == (date(2026, 12, 21), date(2026, 12, 27))


def test_defaults_to_today():
    start, end = last_full_week()

    assert start.weekday() == 0
    assert (end - start).days == 6
    assert end < date.today()
