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

    def _upload_page(error: str | None = None, status: int = 200, doc_date: str = ""):
        # doc_date 를 되돌려 주는 이유: 오류로 화면을 다시 그릴 때 사용자가
        # 골라 둔 날짜까지 날아가면 처음부터 다시 입력해야 한다.
        # today 는 날짜 칸의 max — 브라우저가 미래 날짜를 아예 못 고르게 한다
        # (서버에서도 한 번 더 막지만, 고르기 전에 막는 편이 친절하다).
        return render_template(
            "pdf_upload.html", error=error, doc_date=doc_date,
            today=date.today().isoformat(),
        ), status

    @app.get("/pdf")
    def pdf_upload() -> tuple[str, int]:
        return _upload_page()

    @app.post("/pdf")
    def pdf_check() -> tuple[str, int]:
        file = request.files.get("document")
        raw_date = (request.form.get("doc_date") or "").strip()
        if file is None or not file.filename:
            return _upload_page("파일을 선택해 주세요.", 400, doc_date=raw_date)

        # 기준 시점(선택). 넣으면 "그 뒤로 바뀐 법"만 추려 보여주고,
        # 안 넣으면 예전처럼 인용 목록 전체를 그대로 보여준다.
        ref_date: date | None = None
        if raw_date:
            try:
                ref_date = date.fromisoformat(raw_date)
            except ValueError:
                return _upload_page(
                    f"날짜 형식을 알아볼 수 없어요: {raw_date} — 연-월-일로 골라 주세요.", 400,
                )
            if ref_date > date.today():
                return _upload_page("기준 시점이 미래예요 — 문서를 만든 날짜를 골라 주세요.", 400)

        # 브라우저가 주는 파일명은 NFD(macOS)일 수 있어 표시용으로 NFC 통일
        doc_name = unicodedata.normalize("NFC", file.filename)
        suffix = Path(doc_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            return _upload_page(
                f"지원하지 않는 형식입니다: {suffix or '(확장자 없음)'} — PDF 또는 HWPX만 올릴 수 있어요.",
                400, doc_date=raw_date,
            )

        # pypdfium2/zipfile 은 경로 입력을 받으므로 임시 파일로 내려서 처리.
        #
        # ★ 실측 버그(2026-08-07, Windows 11): 원래 NamedTemporaryFile 을
        #   연 채로 그 이름에 file.save(tmp.name) 을 했는데, Windows 는
        #   NamedTemporaryFile 을 O_TEMPORARY 로 열어 핸들이 살아있는 동안
        #   같은 경로를 다시 열 수 없다 — 업로드마다 PermissionError 로
        #   500 이 났다(POSIX 에서는 재오픈이 되므로 macOS/Linux 에서는
        #   증상이 안 나타난다). 디렉터리만 임시로 잡고 그 안에 우리가
        #   직접 파일을 만들어, 열려 있는 핸들과 경로 재오픈이 겹치지
        #   않게 한다. TemporaryDirectory 는 블록을 벗어날 때 통째로
        #   지우므로 정리 보장은 그대로다.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = str(Path(tmpdir) / f"upload{suffix}")
            file.save(tmp_path)
            try:
                pages = extract_text(tmp_path)
            except Exception:
                # 손상된 파일, 암호 걸린 PDF 등 — 원인 불문 "읽을 수 없음"으로
                return _upload_page(
                    "파일을 읽지 못했어요. 손상됐거나 암호가 걸린 문서일 수 있어요.",
                    400, doc_date=raw_date,
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

        def _change_status(enforce: date | None) -> str:
            """기준 시점 이후 이 법이 바뀌었는지 — "changed"/"unchanged"/"unknown".

            ★ 여기서 쓰는 enforce 는 "지금 시행 중인 버전의 시행일"이다
            (fetch_revision_info 가 change_log 에서 법마다 가장 최근 감지분
            하나를 뽑아 준다). 그래서 이력 전체를 뒤지지 않아도 답이 나온다:

              - 시행일 > 기준일  : 지금 버전이 문서를 쓴 뒤에 시행됐다
                                   => 그 사이에 적어도 한 번 바뀌었다.
              - 시행일 <= 기준일 : 문서를 쓸 때 이미 지금 버전이 시행 중이었고
                                   그 뒤로 바뀐 게 없다(바뀌었다면 더 나중
                                   시행일을 가진 새 버전이 현행이었을 것)
                                   => 확실히 안 바뀌었다.

            ★★ 실측으로 확인하고 설계를 바꾼 부분(2026-08-18): 처음엔
            change_log 에 개정 이력이 쌓여 있는 줄 알고 "기준일 이후 개정
            건수를 센다"로 잡았는데, 실제로는 1909행이 전부 법당 현재 버전
            하나의 중복이었다(고유 (law_id,new_serial_no) 106개, 법당 1개).
            이력이 없어도 위 논리로 예/아니오는 완전히 답할 수 있어서,
            이력 백필 없이 이 방식으로 간다 — 다만 "몇 번, 무엇이" 까지는
            알 수 없으므로 화면에서도 그 이상을 주장하지 않는다(자세한
            내용은 /laws/<id> 전문 비교로 넘긴다).

            DB 조회가 실패했거나 그 법 기록이 없으면 "unknown" — 모르는 것을
            "안 바뀜"으로 뭉개면 안 된다(그게 제일 위험한 오답이다).
            """
            if ref_date is None:
                return "n/a"
            if enforce is None:
                return "unknown"
            return "changed" if enforce > ref_date else "unchanged"

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
                "enforce_date": enforce,
                "revision_type": rev.get("revision_type") or "개정",
                "change_status": _change_status(enforce),
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
            ref_date=ref_date,
            changed=[m for m in matched if m["change_status"] == "changed"],
            unchanged=[m for m in matched if m["change_status"] == "unchanged"],
            unknown=[m for m in matched if m["change_status"] == "unknown"],
        ), 200
