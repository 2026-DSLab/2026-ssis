"""webapp/live.py — 기간 지정 즉석 조회의 캐싱 로직.

실 DB/API/LLM 없이 돈다. _run_live_sweep 자체(실제 국가법령정보 API +
LLM 호출)는 monkeypatch로 대체하고, get_period_result()가 캐시를 언제
재사용하고 언제 다시 계산하는지만 검증한다.
"""

from __future__ import annotations

from datetime import date

import pytest

import webapp.live as live


@pytest.fixture(autouse=True)
def _clear_cache():
    live.clear_cache()
    yield
    live.clear_cache()


def _fake_result(window_key: str, computed_at: float) -> live.PeriodResult:
    return live.PeriodResult(
        window_key=window_key, from_date=date(2026, 7, 1), to_date=date(2026, 7, 20),
        laws=[], hwpx_path=None, computed_at=computed_at, newly_detected=0,
    )


def test_rejects_unknown_window_key():
    with pytest.raises(ValueError, match="5d"):
        live.get_period_result(settings=None, window_key="bogus")


def test_second_call_within_ttl_reuses_cache(monkeypatch):
    calls = []

    def fake_sweep(settings, window_key):
        calls.append(window_key)
        return _fake_result(window_key, computed_at=1_000_000.0)

    monkeypatch.setattr(live, "_run_live_sweep", fake_sweep)
    monkeypatch.setattr(live.time, "time", lambda: 1_000_000.0 + 60)  # 1분 후

    r1 = live.get_period_result(settings=None, window_key="5d")
    r2 = live.get_period_result(settings=None, window_key="5d")

    assert calls == ["5d"]  # 두 번째는 캐시 재사용 — 실제 스윕 다시 안 돎
    assert r1 is r2


def test_call_after_ttl_expires_recomputes(monkeypatch):
    calls = []
    now = {"t": 1_000_000.0}

    def fake_sweep(settings, window_key):
        calls.append(window_key)
        return _fake_result(window_key, computed_at=now["t"])

    monkeypatch.setattr(live, "_run_live_sweep", fake_sweep)
    monkeypatch.setattr(live.time, "time", lambda: now["t"])

    live.get_period_result(settings=None, window_key="5d")
    now["t"] += live.CACHE_TTL_SECONDS + 1  # TTL 만료
    live.get_period_result(settings=None, window_key="5d")

    assert calls == ["5d", "5d"]  # 만료 후엔 다시 계산됨


def test_force_bypasses_cache(monkeypatch):
    calls = []

    def fake_sweep(settings, window_key):
        calls.append(window_key)
        return _fake_result(window_key, computed_at=1_000_000.0)

    monkeypatch.setattr(live, "_run_live_sweep", fake_sweep)
    monkeypatch.setattr(live.time, "time", lambda: 1_000_000.0 + 1)

    live.get_period_result(settings=None, window_key="5d")
    live.get_period_result(settings=None, window_key="5d", force=True)

    assert calls == ["5d", "5d"]


def test_different_window_keys_cached_independently(monkeypatch):
    calls = []

    def fake_sweep(settings, window_key):
        calls.append(window_key)
        return _fake_result(window_key, computed_at=1_000_000.0)

    monkeypatch.setattr(live, "_run_live_sweep", fake_sweep)
    monkeypatch.setattr(live.time, "time", lambda: 1_000_000.0 + 1)

    live.get_period_result(settings=None, window_key="5d")
    live.get_period_result(settings=None, window_key="2w")
    live.get_period_result(settings=None, window_key="5d")  # 캐시 재사용
    live.get_period_result(settings=None, window_key="2w")  # 캐시 재사용

    assert calls == ["5d", "2w"]


def test_period_windows_days_are_correct():
    assert live.PERIOD_WINDOWS == {"5d": 5, "2w": 14, "1m": 30}


# ---------------------------------------------------------------------------
# "이미 요약된 건 LLM 다시 안 부른다" 로직이 쓰는 순수 헬퍼들.
# 실측(2026-08-03, 사용자 지적): 배경 사전 캐싱을 켜면 넓은 기간(1개월)의
# 같은 개정 건이 계속 재조회되는데, LLM을 매번 다시 부르면 완전히 낭비다.
# ---------------------------------------------------------------------------

def _law_change(law_id: str, new_serial_no: str) -> "LawChange":
    from lawtrack.contract.schema import LawChange

    return LawChange(law_id=law_id, law_type="법률", law_name=f"테스트법{law_id}", new_serial_no=new_serial_no)


def _contract(groups: list) -> "WeeklyContract":
    from lawtrack.contract.schema import NoComparisonItem, Period, UnresolvedItem, WeeklyContract

    return WeeklyContract(
        batch_date="2026-08-03", period=Period(from_date="2026-07-01", to_date="2026-08-03"),
        amendment_groups=groups,
        unresolved=[UnresolvedItem(law_id="X", law_name="위치미확정법", new_serial_no="1", reason="0건실패")],
        no_comparison=[NoComparisonItem(law_id="Y", law_name="비교불가법", new_serial_no="1", reason="법령 제정")],
    )


def test_filter_contract_keeps_only_matching_keys():
    from lawtrack.contract.schema import AmendmentGroup

    group = AmendmentGroup(
        group_id="g1",
        laws=[_law_change("A", "1"), _law_change("B", "2"), _law_change("C", "3")],
    )
    contract = _contract([group])

    filtered = live._filter_contract(contract, {("B", "2")})

    assert len(filtered.amendment_groups) == 1
    assert [law.law_id for law in filtered.amendment_groups[0].laws] == ["B"]
    assert filtered.amendment_groups[0].affected_law_ids == ["B"]


def test_filter_contract_drops_group_with_no_matching_laws():
    from lawtrack.contract.schema import AmendmentGroup

    group_keep = AmendmentGroup(group_id="g1", laws=[_law_change("A", "1")])
    group_drop = AmendmentGroup(group_id="g2", laws=[_law_change("B", "2")])
    contract = _contract([group_keep, group_drop])

    filtered = live._filter_contract(contract, {("A", "1")})

    assert len(filtered.amendment_groups) == 1
    assert filtered.amendment_groups[0].group_id == "g1"


def test_filter_contract_preserves_unresolved_and_no_comparison():
    """unresolved/no_comparison은 애초에 LLM이 안 만지는 항목이라, 필터링
    대상(amendment_groups)이 아니라 그대로 통과해야 한다."""
    from lawtrack.contract.schema import AmendmentGroup

    contract = _contract([AmendmentGroup(group_id="g1", laws=[_law_change("A", "1")])])
    filtered = live._filter_contract(contract, set())  # 아무것도 안 남김

    assert filtered.amendment_groups == []
    assert filtered.unresolved == contract.unresolved
    assert filtered.no_comparison == contract.no_comparison


def test_row_to_law_summary_round_trip():
    """DbSink가 쓰는 방향(LawSummary -> asdict -> JSON -> DB)의 정반대를
    한다 — 실제로 DB에 JSON으로 저장됐다가 풀린 모양(딕셔너리)을 그대로
    입력으로 준다."""
    from dataclasses import asdict

    from summarizer.models import ArticleSummary, ArticleUnit, LawSummary

    original = LawSummary(
        law_id="009199", law_name="전자정부법", law_type="법률",
        enforce_date="2026-07-01", revision_type="일부개정",
        source_url="https://example.com", new_serial_no="268103",
        headline="한 줄 요약", body="본문", overview="개정 취지",
        caveats=["주의1"],
        article_summaries=[
            ArticleSummary(
                unit=ArticleUnit(
                    law_id="009199", law_name="전자정부법", location_label="제5조①",
                    change_type="개정", old_text="구", new_text="신", match_status="성공",
                ),
                summary="요약문",
            )
        ],
    )

    # DB round-trip 흉내: asdict()로 풀어 JSON 컬럼처럼 파이썬 dict/list로 온다.
    row = asdict(original)

    rebuilt = live._row_to_law_summary(row)

    assert rebuilt.law_id == original.law_id
    assert rebuilt.headline == original.headline
    assert rebuilt.caveats == original.caveats
    assert len(rebuilt.article_summaries) == 1
    assert rebuilt.article_summaries[0].unit.location_label == "제5조①"
    assert rebuilt.article_summaries[0].summary == "요약문"


def test_bundle_from_rows_wraps_into_contract_summary():
    row = {
        "law_id": "009199", "law_name": "전자정부법", "new_serial_no": "268103",
        "headline": "h", "body": "b", "article_summaries": [], "mappings": [],
        "verifier_issues": [], "caveats": [],
    }
    bundle = live._bundle_from_rows([row], window_key="5d", to_date=date(2026, 8, 3))

    assert bundle.source_file == "live_5d_2026-08-03.json"
    assert bundle.batch_date == "2026-08-03"
    assert len(bundle.laws) == 1
    assert bundle.laws[0].law_id == "009199"


# ---------------------------------------------------------------------------
# 백그라운드 사전 캐싱 — 실 스레드를 시작하되 get_period_result 자체는
# monkeypatch로 대체해 실 API/LLM 없이 "각 프리셋을 도는가/중복 시작을
# 막는가"만 확인한다.
# ---------------------------------------------------------------------------

def test_start_background_refresh_calls_every_window_key(monkeypatch):
    import threading

    calls = []
    stop_event = threading.Event()

    def fake_get_period_result(settings, window_key, force=False):
        calls.append((window_key, force))
        return _fake_result(window_key, computed_at=0.0)

    monkeypatch.setattr(live, "get_period_result", fake_get_period_result)
    live._background_thread = None  # 이전 테스트의 잔여 스레드 참조 제거

    thread = live.start_background_refresh(settings=None, interval_seconds=0.01, stop_event=stop_event)
    stop_event.set()  # 한 바퀴 돈 직후(대기 진입 시점) 바로 멈추게 신호
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert {c[0] for c in calls} == set(live.PERIOD_WINDOWS)
    assert all(force is True for _, force in calls)  # force=True로 불러야 캐시를 실제로 갱신함


def test_start_background_refresh_does_not_start_twice(monkeypatch):
    started = []

    class _FakeThread:
        def __init__(self, *a, **kw):
            started.append(1)
            self._alive = True

        def start(self):
            pass

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(live.threading, "Thread", _FakeThread)
    live._background_thread = None

    t1 = live.start_background_refresh(settings=None)
    t2 = live.start_background_refresh(settings=None)

    assert t1 is t2
    assert len(started) == 1  # 두 번째 호출은 새 스레드를 안 만듦


# ---------------------------------------------------------------------------
# "어떤 단계인지 보여주고 싶어"(2026-08-03) — 논블로킹 진입점 +
# 폴링용 진행상태. 실 DB/API 없이 스레드 동작만 확인한다.
# ---------------------------------------------------------------------------

def test_get_progress_returns_empty_when_never_set():
    assert live.get_progress("5d") == {"stage": "", "detail": ""}


def test_set_progress_then_get_progress_round_trip():
    live._set_progress("5d", "국가법령정보 API 확인 중", "42/102건")
    assert live.get_progress("5d") == {"stage": "국가법령정보 API 확인 중", "detail": "42/102건"}


def test_is_cache_fresh_false_when_nothing_cached():
    assert live.is_cache_fresh("5d") is False


def test_is_cache_fresh_true_right_after_caching(monkeypatch):
    monkeypatch.setattr(live.time, "time", lambda: 1_000_000.0)
    live._cache["5d"] = _fake_result("5d", computed_at=1_000_000.0)
    assert live.is_cache_fresh("5d") is True


def test_ensure_sweep_started_rejects_unknown_window():
    with pytest.raises(ValueError, match="bogus"):
        live.ensure_sweep_started(settings=None, window_key="bogus")


def test_ensure_sweep_started_returns_false_when_cache_fresh(monkeypatch):
    monkeypatch.setattr(live.time, "time", lambda: 1_000_000.0)
    live._cache["5d"] = _fake_result("5d", computed_at=1_000_000.0)

    created = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            created.append(self)

    monkeypatch.setattr(live.threading, "Thread", _FakeThread)

    needs_wait = live.ensure_sweep_started(settings=None, window_key="5d")

    assert needs_wait is False
    assert created == []  # 캐시가 신선하면 스레드를 아예 안 만듦


def test_ensure_sweep_started_starts_thread_when_cache_stale(monkeypatch):
    started_targets = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            started_targets.append(target)
            self._alive = True

        def start(self):
            pass

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(live.threading, "Thread", _FakeThread)

    needs_wait = live.ensure_sweep_started(settings=None, window_key="5d")

    assert needs_wait is True
    assert len(started_targets) == 1


def test_ensure_sweep_started_does_not_start_second_thread_while_running(monkeypatch):
    created = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            created.append(self)
            self._alive = True

        def start(self):
            pass

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(live.threading, "Thread", _FakeThread)

    r1 = live.ensure_sweep_started(settings=None, window_key="5d")
    r2 = live.ensure_sweep_started(settings=None, window_key="5d")

    assert r1 is True and r2 is True
    assert len(created) == 1  # 이미 도는 중이면 두 번째 요청은 스레드를 또 안 만듦


def test_ensure_sweep_started_detects_background_prewarm_already_running(monkeypatch):
    """★★ 실측 발견(2026-08-03, 서버 재시작 직후 curl로 재현): 백그라운드
    미리캐싱 루프가 get_period_result를 직접 불러서 _running_sweeps에
    아무 흔적을 안 남기면, 그 루프가 "5d"를 스윕하는 도중에 방문자가
    /period-check?period=5d를 눌렀을 때 ensure_sweep_started가 "안 도는
    중"으로 오판해 같은 기간을 중복으로 또 스윕한다. 백그라운드 루프도
    자기 자신을 _running_sweeps에 등록해야 이 dedup이 실제로 먹힌다."""
    created = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            created.append(self)

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(live.threading, "Thread", _FakeThread)

    # 백그라운드 루프가 "지금 5d를 스윕 중"이라고 등록한 상태를 흉내낸다
    # (실제로는 start_background_refresh의 _loop가 threading.current_thread()를
    # 등록한다 — 여기선 살아있는 스레드 스텁으로 같은 상황을 재현).
    class _AliveMarker:
        def is_alive(self):
            return True

    live._running_sweeps["5d"] = _AliveMarker()

    needs_wait = live.ensure_sweep_started(settings=None, window_key="5d")

    assert needs_wait is True
    assert created == []  # 이미 백그라운드가 도는 중이면 새 스레드를 또 만들면 안 된다


def test_clear_cache_also_clears_progress_and_running_sweeps():
    live._set_progress("5d", "국가법령정보 API 확인 중", "1/102건")
    live._running_sweeps["5d"] = object()  # 실제 Thread가 아니어도 됨 — 초기화만 확인

    live.clear_cache()

    assert live.get_progress("5d") == {"stage": "", "detail": ""}
    assert live._running_sweeps == {}
