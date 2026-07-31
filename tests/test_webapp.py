"""웹페이지(webapp/app.py) 라우트 테스트.

진짜 DB 없이 돈다 — repo를 가짜로 주입한다(create_app(repo=...)).
법령 요약이 실제로 law_summary에 어떻게 JSON으로 저장/디코딩되는지는
tests/test_summary_db.py가 이미 검증하므로, 여기서는 "그 dict가 웹페이지에
정확히 반영되는가"만 본다.
"""

from __future__ import annotations

from datetime import date

import pytest

import webapp.app as webapp_module
from webapp.app import create_app


class _FakeRepo:
    def __init__(self, *, batch_date: date | None, laws: list[dict] | None = None):
        self._batch_date = batch_date
        self._laws = laws or []

    def latest_batch_date(self):
        return self._batch_date

    def fetch_by_batch(self, batch_date):
        assert batch_date == self._batch_date
        return self._laws


def _law_row(**kw) -> dict:
    base = dict(
        law_id="009199", new_serial_no="268103", law_name="전자정부법",
        law_type="법률", enforce_date="2025-07-08", revision_type="일부개정",
        source_url="https://www.law.go.kr/x", source_file="weekly_contract_2026-07-19_b.json",
        headline="정보시스템 장애관리 체계 강화", overview="이번 개정은 ...",
        caveats=[], article_summaries=[], mappings=[], verifier_issues=[],
    )
    base.update(kw)
    return base


def test_index_shows_empty_state_when_no_batch():
    app = create_app(repo=_FakeRepo(batch_date=None))
    client = app.test_client()

    resp = client.get("/")

    assert resp.status_code == 200
    assert "아직 생성된 배치가 없습니다" in resp.get_data(as_text=True)


def test_index_renders_headline_and_article_row():
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "제56조의2⑤", "change_type": "이동후개정",
                "no_change": False, "moved_from": "②",
            },
            "summary": "정보화 수요 조사 대신 장애관리계획 수립·시행 등을 규칙으로 정하도록 변경",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    client = app.test_client()

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "정보시스템 장애관리 체계 강화" in html
    # location_label은 조/항 구획마다 다른 span으로 쪼개져 렌더링된다(format_location).
    assert '<span class="loc-article">제56조의2</span>' in html
    assert '<span class="loc-clause">⑤</span>' in html
    assert "이동개정" in html  # _TAG 축약 규칙 적용됨
    assert "※ 이동 전 위치: ②" in html


def test_format_location_wraps_branch_numbered_item_as_one_chip():
    """★★ 실측(2026-07-31, 국고금관리법 제10조의2②12의2.): 호가지번호가
    있는 "12의2." 같은 표기는 이전 정규식(\\d+\\.)이 "12"부터 이어지는
    "의2"를 못 잡아, "의2"만 스타일 없는 맨 텍스트로 남아 어색하게
    붙어 보였다. "의N"까지 통째로 하나의 loc-item 칩으로 감싸야 한다."""
    from webapp.app import _format_location

    html = str(_format_location("제10조의2②12의2."))
    assert html == (
        '<span class="loc-article">제10조의2</span>'
        '<span class="loc-clause">②</span>'
        '<span class="loc-item">12의2.</span>'
    )


def test_index_bolds_only_changed_words_in_fulltext():
    """"원문 보기"의 개정 전/후는 어절 단위로 diff해서 바뀐 부분만
    <mark class="diff-changed">로 감싸야 한다 — summarizer.textdiff의
    같은 어절 diff를 재사용(diff_old_html/diff_new_html)."""
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "제20조②", "change_type": "개정", "no_change": False,
                "moved_from": None, "old_text_is_context": False,
                "old_text": "② 위원은 기획재정부 차관으로 한다.",
                "new_text": "② 위원은 기획예산처 차관으로 한다.",
            },
            "summary": "기획재정부가 기획예산처로 변경되었습니다.",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/").get_data(as_text=True)

    assert '<mark class="diff-changed">기획재정부</mark>' in html
    assert '<mark class="diff-changed">기획예산처</mark>' in html
    # 안 바뀐 어절("위원은", "차관으로")은 굵게 감싸면 안 된다.
    assert '<mark class="diff-changed">위원은</mark>' not in html


def test_index_no_diff_highlight_when_old_text_is_context():
    """old_text_is_context=True(구조확장 등)인 경우 old_text가 이 위치의
    진짜 개정 전 문장이 아니므로, new_text를 그것과 diff해서 강조하면
    안 된다 — 원문 그대로(강조 없이) 보여야 한다."""
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "제56조의3①", "change_type": "신설", "no_change": False,
                "moved_from": None, "old_text_is_context": True,
                "old_text": "① 통짜 조문 전체 문장입니다.",
                "new_text": "① 완전히 다른 새 문장입니다.",
            },
            "summary": "새 기준을 마련한다.",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/").get_data(as_text=True)

    assert '<mark class="diff-changed">' not in html
    assert "완전히 다른 새 문장입니다" in html


def test_index_shows_no_change_kind_even_though_change_type_is_amend():
    """no_change=True 인 항목은 change_type("개정")이 아니라 "변경없음"으로
    보여야 한다 — summarizer/report/builder.py의 _kind_of()와 동일 규칙."""
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "제92조③", "change_type": "개정",
                "no_change": True, "moved_from": None,
            },
            "summary": "이 항목은 이번 개정에서 내용이 바뀌지 않았습니다.",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/").get_data(as_text=True)

    assert "변경없음" in html


def test_index_never_renders_highlight_box():
    """★★★ 설계(2026-07-31, 사용자 결정): "확인이 필요한 항목" 박스를
    완전히 없앴다. caveats/verifier_issues가 있어도 화면에 별도 경고
    박스로 노출되면 안 된다 — 데이터 자체(DB)는 남아있어도 웹페이지엔
    안 보여준다."""
    laws = [_law_row(
        article_summaries=[
            {
                "unit": {"location_label": "제56조의3①", "change_type": "신설", "no_change": False, "moved_from": None},
                "summary": "정보시스템 등급산정 기준을 마련한다.",
                "caveats": ["[제56조의3①] 구조확장(구법미분리) — 개정 전 문장이 이 위치에 정확히 대응하지 않음"],
                "error": None,
            }
        ],
        verifier_issues=[
            {"severity": "high", "where": "제5조", "problem": "이동/신설 판정이 원문과 다름"},
        ],
    )]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/").get_data(as_text=True)

    assert 'class="highlight-box"' not in html
    assert "확인이 필요한 항목" not in html


def test_download_serves_hwpx_file(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_module, "REPORTS_DIR", tmp_path)
    hwpx = tmp_path / "weekly_contract_2026-07-19_b.hwpx"
    hwpx.write_bytes(b"fake-hwpx-bytes")

    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=[_law_row()]))
    resp = app.test_client().get("/download")

    assert resp.status_code == 200
    assert resp.data == b"fake-hwpx-bytes"


def test_download_404_when_hwpx_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_module, "REPORTS_DIR", tmp_path)

    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=[_law_row()]))
    resp = app.test_client().get("/download")

    assert resp.status_code == 404


def test_download_404_when_no_batch():
    app = create_app(repo=_FakeRepo(batch_date=None))
    resp = app.test_client().get("/download")

    assert resp.status_code == 404
