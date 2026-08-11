"""webapp/pdfcheck.py — PDF/HWPX 업로드 인용 확인(/pdf) 라우트 테스트.

실 DB/API/LLM 없이 돈다 — 매칭 사전은 repo 의 seed_watchlist.sql 에서
만들어지고, 업로드 문서는 합성 HWPX(메모리에서 조립한 zip)를 쓴다.
PDF 추출 자체의 품질은 test_doc_match.py 의 골드셋 테스트가 담당하므로
여기서는 라우트 계층(수신·검증·렌더링)만 본다.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from webapp.app import create_app


@pytest.fixture(scope="module")
def client():
    app = create_app(repo=object())  # repo 는 /pdf 경로에서 쓰이지 않는다
    app.testing = True
    return app.test_client()


def _hwpx_bytes(text: str) -> io.BytesIO:
    """본문 한 단락짜리 합성 HWPX — extract_hwpx 가 읽는 최소 구조."""
    xml = (
        '<?xml version="1.0"?><hml xmlns:hp="x">'
        f"<hp:p><hp:t>{text}</hp:t></hp:p></hml>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Contents/section0.xml", xml)
        z.writestr("Contents/content.hpf", "")
    buf.seek(0)
    return buf


# ---------- GET /pdf ----------

def test_upload_page_renders(client):
    r = client.get("/pdf")
    assert r.status_code == 200
    assert "인용 확인" in r.get_data(as_text=True)


# ---------- POST /pdf 검증 실패 경로 ----------

def test_post_without_file_is_400(client):
    r = client.post("/pdf", data={})
    assert r.status_code == 400
    assert "파일을 선택해" in r.get_data(as_text=True)


def test_post_wrong_extension_is_400(client):
    r = client.post("/pdf", data={
        "document": (io.BytesIO(b"hello"), "note.txt"),
    })
    assert r.status_code == 400
    assert "지원하지 않는 형식" in r.get_data(as_text=True)


def test_post_corrupt_file_is_400(client):
    # 확장자는 .hwpx 지만 zip 이 아님 → extract 실패 → 사용자 메시지
    r = client.post("/pdf", data={
        "document": (io.BytesIO(b"not a zip at all"), "broken.hwpx"),
    })
    assert r.status_code == 400
    assert "읽지 못했어요" in r.get_data(as_text=True)


# ---------- POST /pdf 매칭 성공 경로 ----------

def test_post_hwpx_matches_watchlist(client):
    body = "본 사업은 「소프트웨어 진흥법」 및 「개인정보 보호법 시행령」에 따른다."
    r = client.post("/pdf", data={
        "document": (_hwpx_bytes(body), "테스트문서.hwpx"),
    })
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "소프트웨어 진흥법" in html
    assert "개인정보 보호법 시행령" in html
    # 페이지 번호가 "뷰어 기준(표지 포함)"임을 결과 화면이 명시해야 한다
    # — 인쇄된 쪽 번호와 오프셋이 있는 문서(실측 4/4)에서의 혼동 방지
    assert "뷰어에서 보이는 순서 기준" in html
    # 시행령이 잡힌 자리에서 법률 본체가 이중 계상되면 안 된다 —
    # "개인정보 보호법" 단독 행이 없어야 함 (law-row 는 시행령 1건뿐)
    assert html.count("law-row-name") == 2  # 소프트웨어 진흥법 + 개인정보 보호법 시행령


def test_post_hwpx_no_match_shows_empty_state(client):
    r = client.post("/pdf", data={
        "document": (_hwpx_bytes("아무 법령도 인용하지 않는 문서다."), "빈문서.hwpx"),
    })
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "인용을 찾지 못했어요" in html


def test_matched_law_links_to_fulltext_page(client):
    # 결과 행이 기존 전문 보기(/laws/<law_id>)로 연결되는지 — 기능 간 배선
    r = client.post("/pdf", data={
        "document": (_hwpx_bytes("「전자정부법」 제2조."), "링크확인.hwpx"),
    })
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "/laws/" in html


def test_scan_warning_on_near_empty_text(client):
    # 텍스트가 거의 없으면(스캔본 PDF 상황) 경고 문구가 떠야 한다
    r = client.post("/pdf", data={
        "document": (_hwpx_bytes("표지"), "스캔본추정.hwpx"),
    })
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "스캔(이미지)" in html


# ---------- 개정 배지 · 요약 한 줄 (revision_lookup 주입) ----------

def _client_with_lookup(lookup):
    app = create_app(repo=object(), revision_lookup=lookup)
    app.testing = True
    return app.test_client()


def _post_sw_law(client_):
    return client_.post("/pdf", data={
        "document": (_hwpx_bytes("「소프트웨어 진흥법」 제20조."), "개정확인.hwpx"),
    })


def test_recent_revision_badge_and_headline_render():
    from datetime import date, timedelta

    def lookup(law_ids):
        return {law_id: {
            "revision_type": "일부개정",
            "enforce_date": date.today() - timedelta(days=10),
            "summary_headline": "품질관리 의무가 강화되었다.",
        } for law_id in law_ids}

    html = _post_sw_law(_client_with_lookup(lookup)).get_data(as_text=True)
    assert "최근 개정 · 일부개정 · 시행" in html
    assert "개정 요약: 품질관리 의무가 강화되었다." in html


def test_old_revision_gets_no_badge_but_headline_stays():
    from datetime import date, timedelta

    def lookup(law_ids):
        return {law_id: {
            "revision_type": "일부개정",
            "enforce_date": date.today() - timedelta(days=400),  # 90일 창 밖
            "summary_headline": "오래된 개정의 요약.",
        } for law_id in law_ids}

    html = _post_sw_law(_client_with_lookup(lookup)).get_data(as_text=True)
    assert "최근 개정" not in html          # 배지 없음
    assert "개정 요약: 오래된 개정의 요약." in html  # 요약은 시기 무관 표시


def test_lookup_failure_degrades_gracefully():
    # DB가 없거나 조회가 예외를 던져도 매칭 결과 자체는 그대로 나와야 한다
    def broken(law_ids):
        raise RuntimeError("DB down")

    r = _post_sw_law(_client_with_lookup(broken))
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "소프트웨어 진흥법" in html
    assert "최근 개정" not in html
    assert "개정 요약:" not in html


def test_oversize_upload_is_413_with_styled_page():
    # ★ 회귀 방지: MAX_CONTENT_LENGTH 를 setdefault 로 걸면 Flask 기본
    # 설정(None)이 이미 있어 조용히 무시된다 — 상한이 실제로 걸리는지,
    # 걸렸을 때 기본 Werkzeug 화면이 아니라 우리 업로드 화면이 나오는지 확인.
    app = create_app(repo=object())
    app.testing = True
    assert app.config["MAX_CONTENT_LENGTH"] is not None  # setdefault 회귀 감지
    app.config["MAX_CONTENT_LENGTH"] = 1000  # 테스트용으로 상한만 낮춤
    r = app.test_client().post("/pdf", data={
        "document": (io.BytesIO(b"0" * 5000), "big.pdf"),
    })
    assert r.status_code == 413
    assert "파일이 너무 커요" in r.get_data(as_text=True)


def test_out_of_watchlist_candidate_listed(client):
    # 감시 대상 외 후보(법령류 접미 + 사전 미등록)가 별도 섹션에 나와야 한다
    r = client.post("/pdf", data={
        "document": (_hwpx_bytes("「지방계약법」을 따른다."), "후보확인.hwpx"),
    })
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "감시 대상 외 인용 후보" in html
    assert "지방계약법" in html
