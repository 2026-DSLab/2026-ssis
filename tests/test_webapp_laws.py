"""webapp/laws.py — 전문 비교 페이지(/laws, /laws/<law_id>) 라우트 테스트.

실 DB/API 없이 돈다. watchlist_repo/version_repo/client_factory를
create_app()에 주입하고, lawtrack.history의 fetch_* 함수는
monkeypatch로 대체한다(test_history.py와 같은 방식).
"""

from __future__ import annotations

from datetime import date

import lawtrack.history as history
from lawtrack.api.fulltext import FullTextResult
from lawtrack.api.oldnew import OldNewResult, VersionInfo
from lawtrack.db.repo import WatchlistEntry
from webapp.app import create_app
from webapp.laws import _apply_diff_highlight


class _FakeWatchlistRepo:
    def __init__(self, entries: list[WatchlistEntry]):
        self._entries = {e.law_id: e for e in entries}

    def active(self):
        return [e for e in self._entries.values() if e.status == "현행"]

    def get(self, law_id):
        return self._entries.get(law_id)


class _FakeVersionRepo:
    def __init__(self):
        self.store: dict[tuple[str, str, str], dict] = {}

    def fetch(self, kind, doc_id, serial_no):
        return self.store.get((kind, doc_id, serial_no))

    def insert_law(self, doc_name, doc_id, serial_no, full_text):
        self.store[("law", doc_id, serial_no)] = full_text

    def insert_admrul(self, doc_name, doc_id, serial_no, full_text):
        self.store[("admrul", doc_id, serial_no)] = full_text


class _FakeClient:
    closed = False

    def close(self):
        self.closed = True


def _entry(**kw) -> WatchlistEntry:
    base = dict(
        law_id="009199", law_type="법률", official_name="전자정부법",
        status="현행", last_serial_no="268103",
    )
    base.update(kw)
    return WatchlistEntry(**base)


def _law_raw(marker: str, *, article_no: str = "1") -> dict:
    return {
        "법령": {"조문": {"조문단위": [{
            "조문번호": article_no, "조문가지번호": "",
            "조문내용": f"제{article_no}조(목적) {marker}", "조문제목": "목적", "조문변경여부": "N",
        }]}}
    }


def _version(serial_no: str) -> VersionInfo:
    return VersionInfo(serial_no, "src", "이름", "20260101", "20251201", "1", "일부개정", False)


def _app(entries, monkeypatch, *, oldnew_map=None, fulltext_by_serial=None, revision_lookup=None):
    oldnew_map = oldnew_map or {}
    fulltext_by_serial = fulltext_by_serial or {}

    def fake_oldnew(client, mst):
        return oldnew_map[mst]

    def fake_fulltext(client, mst):
        raw = fulltext_by_serial[mst]
        return FullTextResult(raw=raw, serial_no=mst, source_id="X", name="X", revision_reason="", revision_text="")

    monkeypatch.setattr(history, "fetch_law_oldnew", fake_oldnew)
    monkeypatch.setattr(history, "fetch_law_fulltext", fake_fulltext)

    return create_app(
        watchlist_repo=_FakeWatchlistRepo(entries),
        version_repo=_FakeVersionRepo(),
        law_api_client_factory=lambda: _FakeClient(),
        # 실 DB 상태와 무관하게 결정론적으로 — 배지 테스트는 직접 주입한다
        revision_lookup=revision_lookup or (lambda ids: {}),
    )


def test_laws_list_shows_all_active_entries():
    app = create_app(
        watchlist_repo=_FakeWatchlistRepo([_entry(law_id="009199", official_name="전자정부법")]),
        version_repo=_FakeVersionRepo(),
        law_api_client_factory=lambda: _FakeClient(),
        revision_lookup=lambda ids: {},  # 실 DB 상태와 무관하게 결정론적으로

    )
    resp = app.test_client().get("/laws")

    assert resp.status_code == 200
    assert "전자정부법" in resp.get_data(as_text=True)


def test_laws_list_excludes_non_active_entries():
    app = create_app(
        watchlist_repo=_FakeWatchlistRepo([
            _entry(law_id="1", official_name="현행법", status="현행"),
            _entry(law_id="2", official_name="폐지법", status="폐지"),
        ]),
        version_repo=_FakeVersionRepo(),
        law_api_client_factory=lambda: _FakeClient(),
        revision_lookup=lambda ids: {},  # 실 DB 상태와 무관하게 결정론적으로

    )
    html = app.test_client().get("/laws").get_data(as_text=True)

    assert "현행법" in html
    assert "폐지법" not in html


def test_law_detail_404_for_unknown_law_id():
    app = create_app(
        watchlist_repo=_FakeWatchlistRepo([]),
        version_repo=_FakeVersionRepo(),
        law_api_client_factory=lambda: _FakeClient(),
        revision_lookup=lambda ids: {},  # 실 DB 상태와 무관하게 결정론적으로

    )
    resp = app.test_client().get("/laws/999999")

    assert resp.status_code == 404


def test_law_detail_shows_two_columns_by_default(monkeypatch):
    """★ 옛날/새 문구를 같은 위치(제1조)에 둬서 강조 대상(어절 diff)이
    되게 한다 — 바뀐 단어만 <mark>로 쪼개져도 "옛날"/"새"라는 단어
    자체는 그대로 남으므로, 그 단어들로 존재를 확인한다("옛날 문구"
    처럼 공백 포함 통짜로 찾으면 마크 태그가 중간에 끼어들어 실패한다)."""
    app = _app(
        [_entry()], monkeypatch,
        oldnew_map={"268103": OldNewResult(True, "", _version("245293"), _version("268103"))},
        fulltext_by_serial={
            "245293": _law_raw("옛날문구"),
            "268103": _law_raw("새문구"),
        },
    )
    html = app.test_client().get("/laws/009199").get_data(as_text=True)

    assert "옛날문구" in html
    assert "새문구" in html
    assert "전전 버전 보기" in html  # depth=2일 땐 더보기 버튼이 항상 보여야 함
    assert "가장 오래된 버전" not in html


def test_law_detail_current_column_shows_recent_revision_badge(monkeypatch):
    """/pdf 결과의 배지를 눌러 넘어온 사용자가 같은 개정 정보를 현재 열
    머리에서 다시 볼 수 있어야 한다(2026-08-11 사용자 요청). 배지 데이터
    소스·90일 규칙은 pdfcheck 와 공유하므로 여기서는 배선만 본다."""
    from datetime import date, timedelta

    recent = date.today() - timedelta(days=10)
    app = _app(
        [_entry()], monkeypatch,
        oldnew_map={"268103": OldNewResult(True, "", _version("245293"), _version("268103"))},
        fulltext_by_serial={
            "245293": _law_raw("옛날문구"),
            "268103": _law_raw("새문구"),
        },
        revision_lookup=lambda ids: {
            "009199": {"revision_type": "일부개정", "enforce_date": recent}},
    )
    html = app.test_client().get("/laws/009199").get_data(as_text=True)

    assert f"최근 개정 · 일부개정 · 시행 {recent}" in html
    # 배지는 현재 열에 1번만 — 개정 전 열에는 붙지 않는다
    assert html.count("최근 개정 ·") == 1


def test_law_detail_depth_3_shows_three_columns(monkeypatch):
    """세 버전을 서로 다른 조번호에 둬서(제1/2/3조) 강조 매칭 대상이
    안 되게 한다 — 이 테스트의 관심사는 강조가 아니라 "3개 버전이 각자
    받아온 내용 그대로 뜨는가"뿐이라, 어절 diff에 얽히지 않게 한다."""
    app = _app(
        [_entry()], monkeypatch,
        oldnew_map={
            "268103": OldNewResult(True, "", _version("245293"), _version("268103")),
            "245293": OldNewResult(True, "", _version("239279"), _version("245293")),
        },
        fulltext_by_serial={
            "239279": _law_raw("가나다라마", article_no="1"),
            "245293": _law_raw("바사아자차", article_no="2"),
            "268103": _law_raw("카타파하까", article_no="3"),
        },
    )
    html = app.test_client().get("/laws/009199?depth=3").get_data(as_text=True)

    assert "가나다라마" in html
    assert "바사아자차" in html
    assert "카타파하까" in html


def test_law_detail_depth_3_shows_notice_when_no_earlier_version(monkeypatch):
    """실측 확인된 체인의 끝(제정본) 케이스 — 오류가 아니라 안내문구여야 한다."""
    app = _app(
        [_entry()], monkeypatch,
        oldnew_map={
            "268103": OldNewResult(False, "no_comparison_field", VersionInfo("", "", "", "", "", "", "", False), _version("268103")),
        },
        fulltext_by_serial={"268103": _law_raw("유일한 버전")},
    )
    html = app.test_client().get("/laws/009199?depth=3").get_data(as_text=True)

    assert "유일한 버전" in html
    assert "더 이전 버전이 없습니다" in html


def test_law_detail_caches_fetched_version_in_repo(monkeypatch):
    """처음 방문 시 받아온 버전이 documents(가짜 repo)에 저장되는지."""
    version_repo = _FakeVersionRepo()

    def fake_oldnew(client, mst):
        return OldNewResult(True, "", _version("245293"), _version("268103"))

    def fake_fulltext(client, mst):
        return FullTextResult(raw=_law_raw(mst), serial_no=mst, source_id="X", name="X", revision_reason="", revision_text="")

    monkeypatch.setattr(history, "fetch_law_oldnew", fake_oldnew)
    monkeypatch.setattr(history, "fetch_law_fulltext", fake_fulltext)

    app = create_app(
        watchlist_repo=_FakeWatchlistRepo([_entry()]),
        version_repo=version_repo,
        law_api_client_factory=lambda: _FakeClient(),
        revision_lookup=lambda ids: {},  # 실 DB 상태와 무관하게 결정론적으로

    )
    app.test_client().get("/laws/009199")

    assert version_repo.fetch("law", "009199", "245293") is not None
    assert version_repo.fetch("law", "009199", "268103") is not None


def test_law_detail_admrul_kind_uses_admrul_oldnew(monkeypatch):
    """watchlist.law_type='행정규칙'이면 admrul 전용 API 경로를 타야 한다
    (실측: MST=/ID= 파라미터를 잘못 맞추면 엉뚱한 응답이 온다)."""
    calls = []

    def fail_if_law_oldnew_called(client, mst):
        raise AssertionError("행정규칙인데 법령용 oldAndNew가 불렸다")

    def fake_admrul_oldnew(client, sid):
        calls.append(sid)
        return OldNewResult(True, "", _version("2100000273620"), _version("2100000280340"))

    def fake_admrul_fulltext(client, sid):
        return FullTextResult(
            raw={"AdmRulService": {"조문내용": [f"제1조(목적) {sid}"]}},
            serial_no=sid, source_id="X", name="X", revision_reason="", revision_text="",
        )

    monkeypatch.setattr(history, "fetch_law_oldnew", fail_if_law_oldnew_called)
    monkeypatch.setattr(history, "fetch_admrul_oldnew", fake_admrul_oldnew)
    monkeypatch.setattr(history, "fetch_admrul_fulltext", fake_admrul_fulltext)

    app = create_app(
        watchlist_repo=_FakeWatchlistRepo([
            _entry(law_id="34470", law_type="행정규칙", official_name="계약집행기준",
                   last_serial_no="2100000280340"),
        ]),
        version_repo=_FakeVersionRepo(),
        law_api_client_factory=lambda: _FakeClient(),
        revision_lookup=lambda ids: {},  # 실 DB 상태와 무관하게 결정론적으로

    )
    resp = app.test_client().get("/laws/34470")

    assert resp.status_code == 200
    assert calls == ["2100000280340"]


def test_law_detail_404_when_no_serial_no_recorded():
    app = create_app(
        watchlist_repo=_FakeWatchlistRepo([_entry(last_serial_no=None)]),
        version_repo=_FakeVersionRepo(),
        law_api_client_factory=lambda: _FakeClient(),
        revision_lookup=lambda ids: {},  # 실 DB 상태와 무관하게 결정론적으로

    )
    resp = app.test_client().get("/laws/009199")

    assert resp.status_code == 404


def test_law_detail_closes_api_client_even_on_error(monkeypatch):
    client = _FakeClient()

    def raise_error(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(history, "fetch_law_oldnew", raise_error)

    app = create_app(
        watchlist_repo=_FakeWatchlistRepo([_entry()]),
        version_repo=_FakeVersionRepo(),
        law_api_client_factory=lambda: client,
        revision_lookup=lambda ids: {},
    )
    app.test_client().get("/laws/009199")  # 500이 나든 말든, 관심사는 client.close() 호출 여부

    assert client.closed is True


def _law_raw_multi(articles: list[tuple[str, str]]) -> dict:
    """[(조번호, marker), ...] -> 여러 조문을 담은 raw JSON."""
    return {
        "법령": {"조문": {"조문단위": [
            {
                "조문번호": no, "조문가지번호": "",
                "조문내용": f"제{no}조(목적) {marker}", "조문제목": "목적", "조문변경여부": "N",
            }
            for no, marker in articles
        ]}}
    }


class TestDiffHighlight:
    """★ 사용자 요청(2026-08-05): 개정 요약 페이지("개정 전/개정 후"
    색 강조)와 같은 방식으로, 전문 비교 페이지도 뭐가 바뀌었는지 색으로
    보여달라는 요청. location_label이 같은데 텍스트만 다르면 어절 단위
    강조(changed), 한쪽에만 있으면 그 줄 전체를 신설/삭제로 표시한다."""

    def test_same_location_different_text_gets_word_level_marks(self, monkeypatch):
        app = _app(
            [_entry()], monkeypatch,
            oldnew_map={"268103": OldNewResult(True, "", _version("245293"), _version("268103"))},
            fulltext_by_serial={
                "245293": _law_raw_multi([("1", "대한올림픽위원회 소관")]),
                "268103": _law_raw_multi([("1", "대한체육회 소관")]),
            },
        )
        html = app.test_client().get("/laws/009199").get_data(as_text=True)

        assert 'class="diff-changed"' in html
        assert "fulltext-col--old" in html
        assert "fulltext-col--new" in html
        assert "compare-line--removed" not in html
        assert "compare-line--added" not in html

    def test_new_only_location_marked_as_added(self, monkeypatch):
        app = _app(
            [_entry()], monkeypatch,
            oldnew_map={"268103": OldNewResult(True, "", _version("245293"), _version("268103"))},
            fulltext_by_serial={
                "245293": _law_raw_multi([("1", "원래 있던 조문")]),
                "268103": _law_raw_multi([("1", "원래 있던 조문"), ("2", "새로 생긴 조문")]),
            },
        )
        html = app.test_client().get("/laws/009199").get_data(as_text=True)

        assert 'compare-line--added' in html
        assert "새로 생긴 조문" in html
        # 안 바뀐 제1조는 강조가 없어야 한다("same" 상태 유지)
        assert 'compare-line--same">원래 있던 조문' in html or 'compare-line--same">' in html

    def test_old_only_location_marked_as_removed(self, monkeypatch):
        app = _app(
            [_entry()], monkeypatch,
            oldnew_map={"268103": OldNewResult(True, "", _version("245293"), _version("268103"))},
            fulltext_by_serial={
                "245293": _law_raw_multi([("1", "남는 조문"), ("2", "없어질 조문")]),
                "268103": _law_raw_multi([("1", "남는 조문")]),
            },
        )
        html = app.test_client().get("/laws/009199").get_data(as_text=True)

        assert "compare-line--removed" in html
        assert "없어질 조문" in html

    def test_oldest_column_in_depth_3_is_not_highlighted(self, monkeypatch):
        """강조는 마지막 두 열(직전 버전 vs 현재)에만 적용한다 — 맨 왼쪽
        (전전) 열은 비교 대상이 아니므로 added/removed/changed 상태가
        전혀 없어야 한다(항상 same)."""
        app = _app(
            [_entry()], monkeypatch,
            oldnew_map={
                "268103": OldNewResult(True, "", _version("245293"), _version("268103")),
                "245293": OldNewResult(True, "", _version("239279"), _version("245293")),
            },
            fulltext_by_serial={
                "239279": _law_raw_multi([("1", "전전 내용만 있음")]),
                "245293": _law_raw_multi([("1", "공통 내용")]),
                "268103": _law_raw_multi([("1", "공통 내용")]),
            },
        )
        html = app.test_client().get("/laws/009199?depth=3").get_data(as_text=True)

        # 전전 열의 문장은 강조 클래스가 하나도 안 붙어야 한다.
        assert 'compare-line--same">전전 내용만 있음' in html


def test_law_detail_strips_amendment_annotations(monkeypatch):
    """실측(2026-08-05, 사용자 리포트): "<개정 2003.12.31>" 같은 각주가
    화면에 그대로 노출됐다. 기존 개정 요약 페이지(law_summary/article_diff
    경로)는 저장 전에 strip_annotations를 이미 적용하므로(repo.py),
    전문 비교 페이지도 같은 기준을 지켜야 한다."""
    app = _app(
        [_entry()], monkeypatch,
        oldnew_map={"268103": OldNewResult(True, "", _version("245293"), _version("268103"))},
        fulltext_by_serial={
            "245293": _law_raw("과오납금 반환결정을 하여야 한다. <개정 2003.12.31>"),
            "268103": _law_raw("과오납금 반환결정을 하여야 한다. <개정 2003.12.31>"),
        },
    )
    html = app.test_client().get("/laws/009199").get_data(as_text=True)

    assert "과오납금 반환결정을 하여야 한다" in html
    assert "2003.12.31" not in html
    assert "&lt;개정" not in html


def test_law_detail_shows_enforce_date_instead_of_serial_no(monkeypatch):
    """사용자 요청(2026-08-05): "2100000272436" 같은 일련번호 대신
    시행일을 보여달라는 요청."""
    app = _app(
        [_entry()], monkeypatch,
        oldnew_map={"268103": OldNewResult(
            True, "",
            VersionInfo("245293", "src", "이름", "20230516", "20230501", "1", "일부개정", False),
            VersionInfo("268103", "src", "이름", "20250708", "20250701", "1", "일부개정", True),
        )},
        fulltext_by_serial={"245293": _law_raw("옛문구"), "268103": _law_raw("새문구")},
    )
    html = app.test_client().get("/laws/009199").get_data(as_text=True)

    assert "2023-05-16" in html
    assert "2025-07-08" in html
    # 일련번호는 title 툴팁에만 남고(참고용), 화면에 보이는 본문 텍스트는 아니다.
    assert 'title="일련번호 245293">2023-05-16<' in html


class TestLinePairingRobustness:
    """짝짓기가 라벨 표기 차이·중복 라벨에 흔들리지 않는지.

    ★ 실측 버그(2026-08-18, 사용자 리포트 — "이 부분은 변한 게 없는데
    색깔 표시가 돼있어", 국가를 당사자로 하는 계약에 관한 법률 시행령):
    두 가지가 겹쳐 있었다.
      1) 같은 항목의 라벨이 버전마다 "1의2." / "1의2" 로 갈려 짝을 못 찾고
         한쪽 '삭제' + 다른 쪽 '신설'로 표시됐다(그 법에서만 18줄).
      2) location_label 이 고유하지 않은데 dict 키로 써서(같은 조에 장
         제목 줄과 본문 줄이 같은 라벨을 갖는 경우가 있다) 뒤 줄이 앞 줄을
         덮어써 비교가 통째로 누락됐다.
    """

    def _cols(self, old_lines, new_lines):
        def col(pairs):
            return {"articles": [{
                "article_label": "제110조",
                "lines": [{"location_label": loc, "text": t,
                           "html": t, "status": "same"} for loc, t in pairs],
            }]}
        return col(old_lines), col(new_lines)

    def test_trailing_dot_in_label_still_pairs(self):
        # 라벨 끝점만 다르고 내용은 같다 => 아무 표시도 없어야 한다
        old, new = self._cols(
            [("제110조②1의2.", "기성부분에 대한 대가 지급과 관련된 사항")],
            [("제110조②1의2", "기성부분에 대한 대가 지급과 관련된 사항")],
        )
        _apply_diff_highlight(old, new)
        assert old["articles"][0]["lines"][0]["status"] == "same"
        assert new["articles"][0]["lines"][0]["status"] == "same"

    def test_trailing_dot_difference_still_detects_real_change(self):
        # 끝점 차이를 무시하되, 내용이 진짜 다르면 변경으로 잡아야 한다
        old, new = self._cols(
            [("제110조②1의2.", "예전 문구")],
            [("제110조②1의2", "새로운 문구")],
        )
        _apply_diff_highlight(old, new)
        assert old["articles"][0]["lines"][0]["status"] == "changed"

    def test_duplicate_labels_pair_in_order_not_overwritten(self):
        # 같은 라벨이 두 줄 => 앞은 앞끼리, 뒤는 뒤끼리 짝지어야 한다.
        # dict 로 덮어쓰면 첫 줄이 사라져 비교가 누락된다.
        old, new = self._cols(
            [("제1조", "제1장 총칙"), ("제1조", "이 영은 …을 규정함을 목적으로 한다")],
            [("제1조", "제1장 총칙"), ("제1조", "이 영은 …을 규정함을 목적으로 한다")],
        )
        _apply_diff_highlight(old, new)
        assert [ln["status"] for ln in old["articles"][0]["lines"]] == ["same", "same"]
        assert [ln["status"] for ln in new["articles"][0]["lines"]] == ["same", "same"]

    def test_duplicate_labels_detect_change_in_second_occurrence(self):
        old, new = self._cols(
            [("제1조", "제1장 총칙"), ("제1조", "예전 목적 조문")],
            [("제1조", "제1장 총칙"), ("제1조", "바뀐 목적 조문")],
        )
        _apply_diff_highlight(old, new)
        assert [ln["status"] for ln in old["articles"][0]["lines"]] == ["same", "changed"]

    def test_extra_duplicate_line_is_added_not_silently_dropped(self):
        old, new = self._cols(
            [("제1조", "첫 줄")],
            [("제1조", "첫 줄"), ("제1조", "새로 붙은 둘째 줄")],
        )
        _apply_diff_highlight(old, new)
        assert [ln["status"] for ln in new["articles"][0]["lines"]] == ["same", "added"]
