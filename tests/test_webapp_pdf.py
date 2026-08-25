"""webapp/pdfcheck.py — PDF/HWPX 업로드 인용 확인(/pdf) 라우트 테스트.

실 DB/API/LLM 없이 돎 — 매칭 사전은 repo 의 seed_watchlist.sql 에서
만들어지고, 업로드 문서는 합성 HWPX(메모리에서 조립한 zip)를 씀.
PDF 추출 자체의 품질은 test_doc_match.py 의 골드셋 테스트가 담당하므로
여기서는 라우트 계층(수신·검증·렌더링)만 봄.
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
    # 페이지 번호가 "뷰어 기준(표지 포함)"임을 결과 화면이 명시해야 함
    # — 인쇄된 쪽 번호와 오프셋이 있는 문서(실측 4/4)에서의 혼동 방지
    assert "뷰어에서 보이는 순서 기준" in html
    # 시행령이 잡힌 자리에서 법률 본체가 이중 계상되면 안 됨 —
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
    # 텍스트가 거의 없으면(스캔본 PDF 상황) 경고 문구가 떠야 함
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
    # DB가 없거나 조회가 예외를 던져도 매칭 결과 자체는 그대로 나와야 함
    def broken(law_ids):
        raise RuntimeError("DB down")

    r = _post_sw_law(_client_with_lookup(broken))
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "소프트웨어 진흥법" in html
    assert "최근 개정" not in html
    assert "개정 요약:" not in html


def test_oversize_upload_is_413_with_styled_page():
    # 회귀 방지: MAX_CONTENT_LENGTH 를 setdefault 로 걸면 Flask 기본
    # 설정(None)이 이미 있어 조용히 무시됨 — 상한이 실제로 걸리는지,
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
    # 감시 대상 외 후보(법령류 접미 + 사전 미등록)가 별도 섹션에 나와야 함
    r = client.post("/pdf", data={
        "document": (_hwpx_bytes("「지방계약법」을 따른다."), "후보확인.hwpx"),
    })
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "감시 대상 외 인용 후보" in html
    assert "지방계약법" in html


# ---------- 기준 시점(문서 작성일) 이후 개정 여부 ----------
#
# 판정 근거는 "지금 시행 중인 버전의 시행일"뿐임(webapp/pdfcheck.py
# _change_status 주석 참고) — change_log 에 개정 이력이 쌓여 있지 않다는
# 실측(1909행이 전부 법당 현재 버전 하나의 중복)을 반영한
# 설계라, 그 전제가 깨지지 않는지 여기서 고정함.

def _post_with_date(client_, doc_date):
    data = {"document": (_hwpx_bytes("「소프트웨어 진흥법」 제20조."), "문서.hwpx")}
    if doc_date is not None:
        data["doc_date"] = doc_date
    return client_.post("/pdf", data=data)


def _lookup_enforced_on(enforce):
    def lookup(law_ids):
        return {law_id: {
            "revision_type": "일부개정",
            "enforce_date": enforce,
            "summary_headline": "요약 한 줄.",
        } for law_id in law_ids}
    return lookup


def test_law_enforced_after_doc_date_is_reported_changed():
    from datetime import date

    c = _client_with_lookup(_lookup_enforced_on(date(2025, 6, 1)))
    html = _post_with_date(c, "2023-05-01").get_data(as_text=True)
    assert "이 문서 이후 개정" in html
    assert "2023-05-01" in html
    assert "이후로 이 문서가 인용한 법령 중" in html


def test_law_enforced_before_doc_date_is_reported_unchanged():
    from datetime import date

    # 문서를 쓸 때 이미 현재 버전이 시행 중이었다 => 그 뒤로 안 바뀜
    c = _client_with_lookup(_lookup_enforced_on(date(2020, 1, 1)))
    html = _post_with_date(c, "2023-05-01").get_data(as_text=True)
    assert "이 문서 이후 개정" not in html
    assert "변동 없는" in html
    assert "개정된 법령은 없어요" in html


def test_missing_revision_info_is_unknown_not_unchanged():
    # 이 기능에서 제일 위험한 오답 방지: 모르는 것을 "안 바뀜"으로
    #   뭉개면 사용자가 낡은 문서를 최신이라고 믿게 됨.
    c = _client_with_lookup(lambda law_ids: {})
    html = _post_with_date(c, "2023-05-01").get_data(as_text=True)
    # 별도 경고 박스는 없앴지만 목록에는 남아야
    # 함 — 확인 못 한 것을 "변동 없음"으로 옮기면 안 됨.
    assert "개정 여부 확인 불가" in html
    assert "소프트웨어 진흥법" in html
    assert "변동 없는" not in html


def test_without_doc_date_behaviour_is_unchanged():
    # 날짜를 안 넣으면 예전처럼 인용 목록 전체만 나옴(구분 없음)
    from datetime import date

    c = _client_with_lookup(_lookup_enforced_on(date(2020, 1, 1)))
    html = _post_with_date(c, None).get_data(as_text=True)
    assert "소프트웨어 진흥법" in html
    assert "변동 없는" not in html
    assert "확인 불가" not in html


def test_malformed_doc_date_is_400():
    c = _client_with_lookup(_lookup_enforced_on(None))
    r = _post_with_date(c, "2023년 5월")
    assert r.status_code == 400
    assert "날짜 형식을 알아볼 수 없어요" in r.get_data(as_text=True)


def test_future_doc_date_is_400():
    from datetime import date, timedelta

    c = _client_with_lookup(_lookup_enforced_on(None))
    r = _post_with_date(c, (date.today() + timedelta(days=1)).isoformat())
    assert r.status_code == 400
    assert "미래" in r.get_data(as_text=True)


def test_doc_date_survives_validation_error():
    # 오류로 화면을 다시 그려도 고른 날짜는 남아 있어야 함
    c = _client_with_lookup(_lookup_enforced_on(None))
    r = c.post("/pdf", data={
        "document": (io.BytesIO(b"x"), "note.txt"),
        "doc_date": "2023-05-01",
    })
    assert r.status_code == 400
    assert 'value="2023-05-01"' in r.get_data(as_text=True)


# ---------- 헤드라인이 "확인 못 함"을 "안 바뀜"으로 단언하지 않는지 ----------
#
# 실측 발견(전수 검증 중): 헤드라인이 어떤 경우에도
#   "N건이 개정됐어요"를 냈음. 인용이 0건이거나 DB 장애로 하나도 확인
#   못 했을 때도 "0건이 개정됐어요"가 떠서, 본문의 "확인 불가"와 정면으로
#   어긋나는 안심 문구가 먼저 눈에 들어왔음.

def test_headline_does_not_claim_zero_revisions_when_nothing_matched():
    c = _client_with_lookup(_lookup_enforced_on(None))
    r = c.post("/pdf", data={
        "document": (_hwpx_bytes("아무 법도 인용하지 않은 문서."), "빈.hwpx"),
        "doc_date": "2023-05-01",
    })
    html = r.get_data(as_text=True)
    assert "0건</strong>이 개정됐어요" not in html
    assert "개정된 법령은 없어요" not in html   # 확인한 적이 없으니 이 말도 안 된다
    assert "찾지 못했어요" in html


def test_headline_says_unverified_when_lookup_fails_entirely():
    def broken(law_ids):
        raise RuntimeError("DB down")

    html = _post_with_date(_client_with_lookup(broken), "2023-05-01").get_data(as_text=True)
    assert "개정 여부를 확인하지 못했어요" in html
    assert "개정된 법령은 없어요" not in html
    # 전부 확인 못 했으면 같은 말을 두 번 하지 않음
    assert html.count("확인하지 못했어요") == 1
    # 경고 박스는 없앴음 — 목록의 표시로만 알림
    assert "안 바뀐 것이 아니라 모르는 것" not in html


def test_headline_flags_partial_unknown_alongside_known_result():
    from datetime import date

    # 한 건은 확인되고(개정됨) 한 건은 기록이 없다 => 둘 다 알려야 함
    def lookup(law_ids):
        return {"011357": {"revision_type": "일부개정",
                           "enforce_date": date(2025, 1, 1),
                           "summary_headline": "요약"}}

    c = _client_with_lookup(lookup)
    r = c.post("/pdf", data={
        "document": (_hwpx_bytes("「개인정보 보호법」 제1조와 「공공감사에 관한 법률」 제3조."), "혼합.hwpx"),
        "doc_date": "2023-05-01",
    })
    html = r.get_data(as_text=True)
    assert "1건</strong>이 개정됐어요" in html
    assert "1건은 확인하지 못했어요" in html
