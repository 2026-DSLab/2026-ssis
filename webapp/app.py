"""Flask 앱 — 최신 배치 요약 페이지 + HWPX 다운로드.

라우트 2개뿐이다:
    GET /          가장 최근 배치(law_summary.batch_date MAX)의 법령별
                    요약을 렌더링한다. 아직 배치가 하나도 없으면 안내만
                    보여준다.
    GET /download  같은 배치가 만든 HWPX 보고서 파일을 내려준다.

★ 왜 파일이 아니라 DB(law_summary)를 보는가: out/summaries/*.json은
"최신이 뭔지"를 파일명·수정시각으로 추론해야 하는데(계약 파일 하나가
여러 데모 조각으로 쪼개질 수도 있어 모호함), law_summary.batch_date는
그 자체로 "이 배치가 언제 것인가"를 확정해 주는 값이라 더 명확하다.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, abort, render_template
from markupsafe import Markup, escape

from lawtrack.config import PROJECT_ROOT, load_settings
from lawtrack.db.conn import Database
from lawtrack.db.repo import LawSummaryRepo
from summarizer.textdiff import diff_segments

REPORTS_DIR = PROJECT_ROOT / "out" / "reports"

#: location_label 은 "제56조의2⑤1.가." 처럼 조/항/호/목이 구분자 없이
#: 붙어 나온다(계약 원본 형식 — src/lawtrack/locate/locator.py 참고).
#: 사람이 읽을 땐 "제56조의2" + "⑤" + "1." + "가."로 눈에 보이는 구획이
#: 있어야 항과 호가 안 헷갈린다. summarizer/report/builder.py의 _ARTICLE
#: 정규식과 같은 경계 규칙을 쓴다 — HWPX 표에서 조문 셀을 세로 병합할 때
#: 쓰는 바로 그 경계다.
_ARTICLE_RE = re.compile(r"^제\d+조(?:의\d+)?")
#: 호/목은 "12의2."(호가지번호)처럼 번호 뒤에 "의N"이 붙는 갈래번호
#: 형태가 있다(실측: 제10조의2②12의2. — 국세기본법). "의N" 부분을
#: 빼먹으면 그 조각만 스타일 없는 맨 텍스트로 남아 어색하게 붙어버린다.
_POS_TOKEN_RE = re.compile(r"[①-⑳]|[가-힣](?:의\d+)?\.|\d+(?:의\d+)?\.|\d+\)")


def _format_location(label: str) -> Markup:
    """location_label 을 조/항/호/목 구획마다 다른 스타일의 span 으로 감싼다."""
    m = _ARTICLE_RE.match(label or "")
    if not m:
        return Markup(escape(label or ""))
    article, rest = m.group(0), label[m.end():]
    out = [f'<span class="loc-article">{escape(article)}</span>']
    pos = 0
    for tm in _POS_TOKEN_RE.finditer(rest):
        if tm.start() > pos:
            out.append(str(escape(rest[pos:tm.start()])))
        tok = tm.group(0)
        if re.fullmatch(r"[①-⑳]", tok):
            cls = "loc-clause"  # 항 — ①②③
        elif re.fullmatch(r"[가-힣](?:의\d+)?\.", tok):
            cls = "loc-subitem"  # 목 — 가.나.다. / 가의2.
        else:
            cls = "loc-item"  # 호 — 1.2.3. / 12의2. / 1)2)
        out.append(f'<span class="{cls}">{escape(tok)}</span>')
        pos = tm.end()
    if pos < len(rest):
        out.append(str(escape(rest[pos:])))
    return Markup("".join(out))

#: "원문 보기"에서 개정 전/후 중 바뀐 어절만 굵게 강조하는 데 쓴다.
#: summarizer.textdiff.diff_segments()는 이미 apply_mappings()의
#: content_ratio 판정·triage 선별에 쓰이는 같은 어절 단위 diff라(2026-07-30
#: 이식), 강조용으로 새 diff 로직을 또 만들지 않고 그대로 재사용한다.
def _diff_html(old_text: str, new_text: str, *, side: str) -> Markup:
    left, right = diff_segments(old_text or "", new_text or "")
    changed_kind = "del" if side == "old" else "ins"
    out = []
    for seg in (left if side == "old" else right):
        text = str(escape(seg.text))
        if seg.kind == changed_kind:
            out.append(f'<mark class="diff-changed">{text}</mark>')
        else:
            out.append(text)
    return Markup("".join(out))


def _diff_old_html(old_text: str, new_text: str) -> Markup:
    return _diff_html(old_text, new_text, side="old")


def _diff_new_html(old_text: str, new_text: str) -> Markup:
    return _diff_html(old_text, new_text, side="new")


#: summarizer/report/builder.py의 _TAG와 같은 축약 규칙이다. 그대로
#: import하지 않는 이유: builder.py는 무거운 hwpx 패키지를 최상단에서
#: import해서, 가벼워야 할 웹페이지 렌더링에 불필요한 의존성이 끌려온다.
#: HWPX 보고서와 웹페이지가 같은 용어를 쓰도록 값만 그대로 복제한다 —
#: 둘 중 하나를 고치면 반드시 다른 쪽도 맞춰야 한다.
_TAG = {
    "신설": "신설",
    "개정": "개정",
    "삭제": "삭제",
    "이동": "이동",
    "이동후개정": "이동개정",
    "미상": "변경",
}


def _build_repo() -> LawSummaryRepo:
    settings = load_settings()
    db = Database(settings.db)
    return LawSummaryRepo(db)


def _kind_of(article_summary: dict) -> str:
    """"구분" 칸 — summarizer/report/builder.py의 _kind_of()와 동일한 규칙.

    unit.no_change 를 change_type 보다 먼저 본다: 예를 들어 같은 조문의
    다른 항만 바뀌어 change_type 은 "개정"으로 남아있어도, 이 항목 자체는
    안 바뀐 경우(no_change=True)엔 "변경없음"으로 보여야 한다 — HWPX
    보고서와 웹페이지가 같은 데이터를 다르게 보여주면 안 되므로 규칙을
    그대로 복제했다.
    """
    unit = article_summary.get("unit", {})
    if unit.get("no_change"):
        return "변경없음"
    change_type = unit.get("change_type", "")
    return _TAG.get(change_type, change_type)


#: kind_of()가 돌려주는 한글 라벨 → 배지 색 클래스. 템플릿에서 badge--{{ }}
#: 형태로만 쓰이므로 여기 없는 라벨은 자동으로 무채색 배지가 된다.
_KIND_CSS = {
    "신설": "new",
    "개정": "amend",
    "삭제": "delete",
    "이동": "move",
    "이동개정": "move",
    "변경": "other",
    "변경없음": "nochange",
}


def _kind_css(article_summary: dict) -> str:
    return _KIND_CSS.get(_kind_of(article_summary), "other")


def _kind_css_for_label(kind_label: str) -> str:
    """통계 배지처럼 article_summary dict 없이 라벨(예: '신설')만 있을 때."""
    return _KIND_CSS.get(kind_label, "other")


#: law.source_url(DB) 은 src/lawtrack/contract/export.py의 _source_url() 이
#: 만든 DRF(Data Reference API) 링크(?target=law&MST=...)다 — OC 인증키를
#: 일부러 뺐으므로(export.py 주석 참고, 키 유출 방지) 그 자체로는 열리지
#: 않는다. law.go.kr은 인증키 없이도 "/법령/{법령명}", "/행정규칙/{명}"
#: 같은 이름 기반 공개 URL을 지원한다(실측: 전자정부법/전자정부법 시행령/
#: 행정규칙 이름 모두 200 확인) — 웹페이지에선 이걸 대신 쓴다.
def _public_law_url(law_type: str, law_name: str) -> str:
    prefix = "행정규칙" if law_type == "행정규칙" else "법령"
    return f"https://www.law.go.kr/{prefix}/{quote(law_name)}"


def _summary_stats(laws: list[dict]) -> dict:
    """헤더 상단 통계 배지용 — 구분별 조문 건수 + 총계.

    HWPX 보고서엔 없는, 웹페이지라서 가능한 "한눈에 보기" 요약이다.
    """
    counts: dict[str, int] = {}
    total = 0
    for law in laws:
        for a in law.get("article_summaries", []):
            kind = _kind_of(a)
            counts[kind] = counts.get(kind, 0) + 1
            total += 1
    order = ["신설", "개정", "삭제", "이동", "이동개정", "변경없음", "변경"]
    breakdown = [(k, counts[k]) for k in order if k in counts]
    breakdown += [(k, v) for k, v in counts.items() if k not in order]
    return {"total_laws": len(laws), "total_articles": total, "breakdown": breakdown}


def create_app(repo: LawSummaryRepo | None = None) -> Flask:
    """앱 팩토리. repo를 주입할 수 있어 테스트에서 진짜 DB 없이 확인 가능하다."""
    app = Flask(__name__)
    app.jinja_env.globals["kind_of"] = _kind_of
    app.jinja_env.globals["kind_css"] = _kind_css
    app.jinja_env.globals["kind_css_for_label"] = _kind_css_for_label
    app.jinja_env.globals["format_location"] = _format_location
    app.jinja_env.globals["public_law_url"] = _public_law_url
    app.jinja_env.globals["diff_old_html"] = _diff_old_html
    app.jinja_env.globals["diff_new_html"] = _diff_new_html
    _repo = repo if repo is not None else _build_repo()

    @app.get("/")
    def index() -> str:
        batch_date = _repo.latest_batch_date()
        if batch_date is None:
            return render_template("report.html", batch_date=None, laws=[], stats=None)
        laws = _repo.fetch_by_batch(batch_date)
        stats = _summary_stats(laws)
        return render_template("report.html", batch_date=batch_date, laws=laws, stats=stats)

    @app.get("/download")
    def download() -> Response:
        from flask import send_file

        batch_date = _repo.latest_batch_date()
        if batch_date is None:
            abort(404, "아직 생성된 배치가 없습니다.")
        laws = _repo.fetch_by_batch(batch_date)
        source_file = next((law["source_file"] for law in laws if law.get("source_file")), None)
        if not source_file:
            abort(404, "이 배치의 원본 계약 파일 정보를 찾을 수 없습니다.")
        hwpx_path = REPORTS_DIR / f"{Path(source_file).stem}.hwpx"
        if not hwpx_path.exists():
            abort(404, f"HWPX 보고서 파일이 없습니다: {hwpx_path.name} (--hwpx 로 생성했는지 확인)")
        return send_file(
            hwpx_path, as_attachment=True, download_name=hwpx_path.name,
            mimetype="application/octet-stream",
        )

    return app


if __name__ == "__main__":
    create_app().run(debug=True)
