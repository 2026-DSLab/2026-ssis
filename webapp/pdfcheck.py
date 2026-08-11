"""PDF/HWPX 업로드 → 감시 대상(watchlist 102건) 인용 확인 페이지.

업로드된 문서에서 텍스트를 뽑아(doc_match.extract) 감시 대상 법령·행정
규칙이 어디에 몇 번 인용되는지 찾아(doc_match.match) 그대로 보여준다.
전 과정이 요청 처리 중 메모리에서 끝난다 — DB에 아무것도 저장하지
않고, 외부 API/LLM도 부르지 않는다(결정론적 문자열 매칭뿐).

매칭 사전은 database/seed_watchlist.sql 을 파싱해 만든다(doc_match
PoC와 동일). watchlist 테이블 SELECT 로 바꿀 수 있는 동일 인터페이스
지만, seed 기반을 유지하면 DB 없이도 이 페이지가 동작하고 PoC 실측
결과와 1:1 대조 검증이 가능해 일단 그대로 둔다.
"""

from __future__ import annotations

import logging
import tempfile
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from flask import Flask, render_template, request

from doc_match.dictionary import Dictionary, load_from_seed
from doc_match.extract import extract_text
from doc_match.match import match_pages
from doc_match.report import build_summary

log = logging.getLogger(__name__)

#: 허용 확장자 — doc_match.extract 가 지원하는 형식과 정확히 같다.
ALLOWED_SUFFIXES = {".pdf", ".hwpx"}

#: 업로드 상한(50MB). 실측 예시 문서 4개가 2.8~10MB 라 여유 5배로 잡음.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: 추출 텍스트가 이보다 적으면 "스캔본(이미지) PDF" 경고를 띄운다
#: (scripts/check_document.py 의 경고 기준과 동일).
SCAN_WARNING_CHARS = 100

_SEED_PATH = Path(__file__).resolve().parents[1] / "database" / "seed_watchlist.sql"

#: "최근 개정" 배지 기준 — 최신 개정의 시행일이 이 기간 안(또는 미래 =
#: 시행 예정)일 때만 배지를 단다. 실측(2026-08-11, watchlist 102건):
#: 90일 창이면 23건에만 붙어 "전부 배지"가 되는 노이즈를 피할 수 있다.
RECENT_REVISION_DAYS = 90


def fetch_revision_info(law_ids: list[str]) -> dict[str, dict]:
    """법령들의 최신 개정 정보 + (있으면) 요약 한 줄을 DB에서 가져온다.

    /pdf 결과의 배지·요약 한 줄과 전문 보기(/laws/<id>)의 현재 열 배지가
    같은 정보를 보여줘야 하므로(사용자가 배지를 눌러 넘어온 맥락 유지)
    이 함수 하나를 두 라우트가 공유한다.

    {law_id: {"revision_type", "enforce_date", "summary_headline"}} 반환.
    DB가 없거나 조회에 실패하면 빈 dict — 업로드 매칭 기능 자체는 DB 없이
    끝까지 동작해야 하므로(결정론적 문자열 매칭뿐), 개정 정보는 "있으면
    더해지는" 부가 정보로만 다룬다.

    DISTINCT ON (law_id) + detected_at 역순: 법령마다 가장 최근 감지분
    하나만 고른다 — change_log 에 같은 개정이 중복 기록돼 있어도(실측:
    최초 채움 날 백그라운드 스윕 경쟁으로 97쌍 중복 발생) 안전하다.
    """
    if not law_ids:
        return {}
    try:
        from lawtrack.config import load_settings
        from lawtrack.db.conn import Database

        db = Database(load_settings().db)
        with db.cursor() as (_, cur):
            cur.execute(
                """
                SELECT DISTINCT ON (c.law_id)
                       c.law_id, c.revision_type, c.enforce_date,
                       s.headline AS summary_headline
                FROM change_log c
                LEFT JOIN law_summary s
                  ON s.law_id = c.law_id AND s.new_serial_no = c.new_serial_no
                WHERE c.law_id = ANY(%s)
                ORDER BY c.law_id, c.detected_at DESC, c.id DESC
                """,
                (list(law_ids),),
            )
            return {r["law_id"]: dict(r) for r in cur.fetchall()}
    except Exception as exc:  # DB 미기동·설정 없음 등 — 매칭 결과는 그대로 낸다
        log.warning("개정 정보 조회 실패(매칭 결과만 표시): %s", exc)
        return {}


def _pages_label(pages: list[int], limit: int = 10) -> str:
    """[1,3,5,...] → "p.1, 3, 5 외 2p" — CLI 리포트(render_text)와 같은 표기."""
    shown = ", ".join(map(str, pages[:limit]))
    more = f" 외 {len(pages) - limit}p" if len(pages) > limit else ""
    return f"p.{shown}{more}"


def register_pdf_routes(
    app: Flask, *,
    seed_path: Path | str | None = None,
    revision_lookup: Callable[[list[str]], dict[str, dict]] | None = None,
) -> None:
    """라우트 2개(GET/POST /pdf)를 등록한다.

    사전은 첫 요청에서 한 번만 만들어 재사용한다(지연 생성) —
    register_law_routes 와 같은 이유로, create_app() 호출만으로는
    아무 비용도 치르지 않게 한다.
    """
    _lazy: dict[str, Dictionary] = {}
    _seed = Path(seed_path) if seed_path else _SEED_PATH
    _revisions = revision_lookup or fetch_revision_info

    # 업로드 본문 크기 상한. Flask 전역 설정이지만 이 앱에 다른 업로드
    # 라우트가 없어 부작용이 없고, 상한이 아예 없으면 대용량 전송으로
    # 메모리가 터질 수 있어 여기서 걸어 둔다(초과 시 413).
    # ★ setdefault 를 쓰면 안 된다 — Flask 기본 설정에 이 키가 이미
    #   None 으로 존재해서 setdefault 가 조용히 아무것도 안 한다(실측).
    if app.config.get("MAX_CONTENT_LENGTH") is None:
        app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    @app.errorhandler(413)
    def _too_large(_e):
        # 기본 413 페이지는 스타일 없는 Werkzeug 화면이라, 업로드 화면에
        # 같은 디자인의 에러 문구로 되돌려 준다.
        return _upload_page(
            f"파일이 너무 커요 — {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 이하만 올릴 수 있어요.", 413,
        )

    def _dictionary() -> Dictionary:
        if "d" not in _lazy:
            _lazy["d"] = load_from_seed(str(_seed))
        return _lazy["d"]

    def _upload_page(error: str | None = None, status: int = 200):
        return render_template("pdf_upload.html", error=error), status

    @app.get("/pdf")
    def pdf_upload() -> tuple[str, int]:
        return _upload_page()

    @app.post("/pdf")
    def pdf_check() -> tuple[str, int]:
        file = request.files.get("document")
        if file is None or not file.filename:
            return _upload_page("파일을 선택해 주세요.", 400)

        # 브라우저가 주는 파일명은 NFD(macOS)일 수 있어 표시용으로 NFC 통일
        doc_name = unicodedata.normalize("NFC", file.filename)
        suffix = Path(doc_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            return _upload_page(
                f"지원하지 않는 형식입니다: {suffix or '(확장자 없음)'} — PDF 또는 HWPX만 올릴 수 있어요.",
                400,
            )

        # pypdfium2/zipfile 은 경로 입력을 받으므로 임시 파일로 내려서 처리
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            file.save(tmp.name)
            try:
                pages = extract_text(tmp.name)
            except Exception:
                # 손상된 파일, 암호 걸린 PDF 등 — 원인 불문 "읽을 수 없음"으로
                return _upload_page(
                    "파일을 읽지 못했어요. 손상됐거나 암호가 걸린 문서일 수 있어요.", 400,
                )

        d = _dictionary()
        summary = build_summary(match_pages(pages, d), d)

        total_chars = sum(len(p) for p in pages)

        # 매칭된 법령의 최신 개정 정보(있으면). 배지는 "최근 90일 내 시행
        # 또는 시행 예정"에만 — 오래된 개정까지 전부 달면 배지가 신호가
        # 아니라 소음이 된다. 요약 한 줄은 시기 무관하게 있으면 보여준다.
        try:
            revisions = _revisions([m["law_id"] for m in summary["matched"]])
        except Exception as exc:  # 부가 정보 실패가 매칭 결과를 막으면 안 된다
            log.warning("개정 정보 조회 실패(매칭 결과만 표시): %s", exc)
            revisions = {}
        cutoff = date.today() - timedelta(days=RECENT_REVISION_DAYS)

        def _annotate(m: dict) -> dict:
            rev = revisions.get(m["law_id"]) or {}
            enforce = rev.get("enforce_date")
            recent = enforce is not None and enforce >= cutoff
            return {
                **m,
                "pages_label": _pages_label(m["pages"]),
                "recent_revision": (
                    {"revision_type": rev.get("revision_type") or "개정",
                     "enforce_date": enforce}
                    if recent else None
                ),
                "summary_headline": rev.get("summary_headline"),
            }

        matched = [_annotate(m) for m in summary["matched"]]
        candidates = [
            {**c, "pages_label": _pages_label(c["pages"])}
            for c in summary["out_of_watchlist_candidates"]
        ]
        return render_template(
            "pdf_result.html",
            doc_name=doc_name,
            page_count=len(pages),
            watchlist_total=summary["watchlist_total"],
            matched=matched,
            candidates=candidates,
            scan_warning=total_chars < SCAN_WARNING_CHARS,
        ), 200
