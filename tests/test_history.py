"""lawtrack.history — 전문 비교 페이지가 쓰는 버전 체인 조회+캐싱 테스트.

실 API/DB 없이 돈다. fetch_law_oldnew/fetch_law_fulltext 등은 모듈
네임스페이스에서 monkeypatch로 대체하고, VersionRepo는 딕셔너리 기반
가짜로 대체한다.
"""

from __future__ import annotations

import lawtrack.history as history
from lawtrack.api.oldnew import OldNewResult, VersionInfo
from lawtrack.api.fulltext import FullTextResult


class _FakeRepo:
    """documents 테이블 흉내 — {(kind, doc_id, serial_no): full_text}."""

    def __init__(self):
        self._store: dict[tuple[str, str, str], dict] = {}

    def fetch(self, kind, doc_id, serial_no):
        return self._store.get((kind, doc_id, serial_no))

    def insert_law(self, doc_name, doc_id, serial_no, full_text):
        self._store[("law", doc_id, serial_no)] = full_text

    def insert_admrul(self, doc_name, doc_id, serial_no, full_text):
        self._store[("admrul", doc_id, serial_no)] = full_text


def _version(serial_no: str, *, enforce_date: str = "20260101", promulgation_date: str = "20251201") -> VersionInfo:
    return VersionInfo(serial_no, "src", "이름", enforce_date, promulgation_date, "1", "일부개정", False)


class TestKindOf:
    def test_admrul_law_type_maps_to_admrul_kind(self):
        assert history.kind_of("행정규칙") == "admrul"

    def test_other_law_types_map_to_law_kind(self):
        assert history.kind_of("법률") == "law"
        assert history.kind_of("시행령") == "law"
        assert history.kind_of("시행규칙") == "law"


class TestGetPreviousSerial:
    def test_returns_old_serial_when_available(self, monkeypatch):
        def fake_fetch_law_oldnew(client, mst):
            assert mst == "268103"
            return OldNewResult(True, "", _version("245293"), _version("268103"))

        monkeypatch.setattr(history, "fetch_law_oldnew", fake_fetch_law_oldnew)

        assert history.get_previous_serial(client=None, kind="law", serial_no="268103") == "245293"

    def test_returns_none_at_first_ever_version(self, monkeypatch):
        """실측(2026-08-05, 스파이크): 제정본까지 거슬러 올라가면
        신구법존재여부="N"으로 온다 — 오류가 아니라 체인의 정상적인 끝."""
        def fake_fetch_law_oldnew(client, mst):
            return OldNewResult(False, "no_comparison_field", VersionInfo("", "", "", "", "", "", "", False), _version(mst))

        monkeypatch.setattr(history, "fetch_law_oldnew", fake_fetch_law_oldnew)

        assert history.get_previous_serial(client=None, kind="law", serial_no="239279") is None

    def test_uses_admrul_fetcher_for_admrul_kind(self, monkeypatch):
        calls = []

        def fake_fetch_admrul_oldnew(client, sid):
            calls.append(sid)
            return OldNewResult(True, "", _version("2100000273620"), _version("2100000280340"))

        monkeypatch.setattr(history, "fetch_admrul_oldnew", fake_fetch_admrul_oldnew)

        result = history.get_previous_serial(client=None, kind="admrul", serial_no="2100000280340")

        assert result == "2100000273620"
        assert calls == ["2100000280340"]


class TestGetOrFetchFullText:
    def test_returns_cached_without_calling_api(self, monkeypatch):
        repo = _FakeRepo()
        repo._store[("law", "009199", "268103")] = {"법령": "캐시된 전문"}

        def fail_if_called(*a, **kw):
            raise AssertionError("캐시가 있으면 실 API를 부르면 안 된다")

        monkeypatch.setattr(history, "fetch_law_fulltext", fail_if_called)

        result = history.get_or_fetch_full_text(
            client=None, repo=repo, kind="law", doc_id="009199",
            doc_name="전자정부법", serial_no="268103",
        )

        assert result == {"법령": "캐시된 전문"}

    def test_fetches_and_caches_when_missing(self, monkeypatch):
        repo = _FakeRepo()

        def fake_fetch(client, mst):
            return FullTextResult(raw={"법령": "새 전문"}, serial_no=mst, source_id="009199", name="전자정부법", revision_reason="", revision_text="")

        monkeypatch.setattr(history, "fetch_law_fulltext", fake_fetch)

        result = history.get_or_fetch_full_text(
            client=None, repo=repo, kind="law", doc_id="009199",
            doc_name="전자정부법", serial_no="245293",
        )

        assert result == {"법령": "새 전문"}
        assert repo.fetch("law", "009199", "245293") == {"법령": "새 전문"}  # 캐싱됨


class TestBuildVersionChain:
    def test_depth_2_returns_oldest_to_newest(self, monkeypatch):
        """실측 확인된 전자정부법 체인(245293 -> 268103)을 흉내낸다."""
        def fake_oldnew(client, mst):
            assert mst == "268103"
            return OldNewResult(True, "", _version("245293"), _version("268103"))

        def fake_fulltext(client, mst):
            return FullTextResult(raw={"serial": mst}, serial_no=mst, source_id="009199", name="전자정부법", revision_reason="", revision_text="")

        monkeypatch.setattr(history, "fetch_law_oldnew", fake_oldnew)
        monkeypatch.setattr(history, "fetch_law_fulltext", fake_fulltext)

        chain = history.build_version_chain(
            client=None, repo=_FakeRepo(), kind="law", doc_id="009199",
            doc_name="전자정부법", current_serial="268103", depth=2,
        )

        assert [v.serial_no for v in chain] == ["245293", "268103"]  # 오래된 -> 최신

    def test_depth_3_chains_two_hops_back(self, monkeypatch):
        """실측 확인된 3단 체인(239279 -> 245293 -> 268103)을 흉내낸다."""
        oldnew_map = {
            "268103": OldNewResult(True, "", _version("245293"), _version("268103")),
            "245293": OldNewResult(True, "", _version("239279"), _version("245293")),
        }

        def fake_oldnew(client, mst):
            return oldnew_map[mst]

        def fake_fulltext(client, mst):
            return FullTextResult(raw={"serial": mst}, serial_no=mst, source_id="009199", name="전자정부법", revision_reason="", revision_text="")

        monkeypatch.setattr(history, "fetch_law_oldnew", fake_oldnew)
        monkeypatch.setattr(history, "fetch_law_fulltext", fake_fulltext)

        chain = history.build_version_chain(
            client=None, repo=_FakeRepo(), kind="law", doc_id="009199",
            doc_name="전자정부법", current_serial="268103", depth=3,
        )

        assert [v.serial_no for v in chain] == ["239279", "245293", "268103"]

    def test_stops_early_when_no_earlier_version_exists(self, monkeypatch):
        """depth=3을 요청했지만 한 단계만에 제정본에 닿으면, 체인은
        depth보다 짧게 끝나야 한다(오류를 내지 않고)."""
        def fake_oldnew(client, mst):
            return OldNewResult(False, "no_comparison_field", VersionInfo("", "", "", "", "", "", "", False), _version(mst))

        def fake_fulltext(client, mst):
            return FullTextResult(raw={"serial": mst}, serial_no=mst, source_id="X", name="X", revision_reason="", revision_text="")

        monkeypatch.setattr(history, "fetch_law_oldnew", fake_oldnew)
        monkeypatch.setattr(history, "fetch_law_fulltext", fake_fulltext)

        chain = history.build_version_chain(
            client=None, repo=_FakeRepo(), kind="law", doc_id="009199",
            doc_name="전자정부법", current_serial="200000", depth=3,
        )

        assert [v.serial_no for v in chain] == ["200000"]  # 자기 자신뿐

    def test_reuses_cache_and_does_not_refetch_already_stored_version(self, monkeypatch):
        repo = _FakeRepo()
        repo._store[("law", "009199", "245293")] = {"cached": True}

        def fake_oldnew(client, mst):
            return OldNewResult(True, "", _version("245293"), _version("268103"))

        fulltext_calls = []

        def fake_fulltext(client, mst):
            fulltext_calls.append(mst)
            return FullTextResult(raw={"serial": mst}, serial_no=mst, source_id="009199", name="전자정부법", revision_reason="", revision_text="")

        monkeypatch.setattr(history, "fetch_law_oldnew", fake_oldnew)
        monkeypatch.setattr(history, "fetch_law_fulltext", fake_fulltext)

        chain = history.build_version_chain(
            client=None, repo=repo, kind="law", doc_id="009199",
            doc_name="전자정부법", current_serial="268103", depth=2,
        )

        assert chain[0].full_text == {"cached": True}
        assert fulltext_calls == ["268103"]  # 245293은 캐시 히트라 API 호출 없음


class TestBuildVersionChainCapturesDates:
    """★ 사용자 요청(2026-08-05): 화면에 일련번호 대신 시행일/공포일을
    보여달라는 요청 — oldAndNew 응답에 이미 실려 오는 날짜를 체인을
    걸으면서 같이 챙긴다(추가 API 호출 없이)."""

    def test_depth_2_captures_dates_for_both_versions_from_single_call(self, monkeypatch):
        def fake_oldnew(client, mst):
            return OldNewResult(
                True, "",
                _version("245293", enforce_date="20230516"),
                _version("268103", enforce_date="20250708"),
            )

        def fake_fulltext(client, mst):
            return FullTextResult(raw={"serial": mst}, serial_no=mst, source_id="X", name="X", revision_reason="", revision_text="")

        monkeypatch.setattr(history, "fetch_law_oldnew", fake_oldnew)
        monkeypatch.setattr(history, "fetch_law_fulltext", fake_fulltext)

        chain = history.build_version_chain(
            client=None, repo=_FakeRepo(), kind="law", doc_id="009199",
            doc_name="전자정부법", current_serial="268103", depth=2,
        )

        by_serial = {v.serial_no: v for v in chain}
        assert by_serial["245293"].enforce_date == "20230516"
        assert by_serial["268103"].enforce_date == "20250708"

    def test_depth_3_captures_distinct_dates_per_hop(self, monkeypatch):
        oldnew_map = {
            "268103": OldNewResult(True, "", _version("245293", enforce_date="20230516"), _version("268103", enforce_date="20250708")),
            "245293": OldNewResult(True, "", _version("239279", enforce_date="20220712"), _version("245293", enforce_date="20230516")),
        }

        def fake_oldnew(client, mst):
            return oldnew_map[mst]

        def fake_fulltext(client, mst):
            return FullTextResult(raw={"serial": mst}, serial_no=mst, source_id="X", name="X", revision_reason="", revision_text="")

        monkeypatch.setattr(history, "fetch_law_oldnew", fake_oldnew)
        monkeypatch.setattr(history, "fetch_law_fulltext", fake_fulltext)

        chain = history.build_version_chain(
            client=None, repo=_FakeRepo(), kind="law", doc_id="009199",
            doc_name="전자정부법", current_serial="268103", depth=3,
        )

        assert [v.enforce_date for v in chain] == ["20220712", "20230516", "20250708"]

    def test_missing_date_info_falls_back_to_empty_string(self, monkeypatch):
        """depth=3인데 체인이 1개뿐으로 끝나도(제정본) 최소한 자기 자신의
        날짜는 채워져야 한다 — new_version은 flag='N'이어도 채워진다."""
        def fake_oldnew(client, mst):
            return OldNewResult(
                False, "no_comparison_field",
                VersionInfo("", "", "", "", "", "", "", False),
                _version(mst, enforce_date="20220101"),
            )

        def fake_fulltext(client, mst):
            return FullTextResult(raw={"serial": mst}, serial_no=mst, source_id="X", name="X", revision_reason="", revision_text="")

        monkeypatch.setattr(history, "fetch_law_oldnew", fake_oldnew)
        monkeypatch.setattr(history, "fetch_law_fulltext", fake_fulltext)

        chain = history.build_version_chain(
            client=None, repo=_FakeRepo(), kind="law", doc_id="009199",
            doc_name="전자정부법", current_serial="200000", depth=3,
        )

        assert len(chain) == 1
        assert chain[0].enforce_date == "20220101"
