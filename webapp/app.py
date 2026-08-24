"""Flask 앱 — 최신 배치 요약 페이지 + HWPX 다운로드.

라우트:
    GET /               가장 최근 배치(law_summary.batch_date MAX)의
                         법령별 요약을 렌더링함. period 쿼리파라미터가
                         있으면 대신 기간 즉석 조회 결과를 보여줌.
    GET /download       같은 배치(또는 period)가 만든 HWPX 보고서 파일을
                         내려줌.
    GET /period-check   기간 즉석 조회를 논블로킹으로 시작만 시킴 —
                         캐시가 신선하면 {"ready": true}, 아니면 백그라운드
                         스윕을 시작하고 {"ready": false}. 로딩 화면이
                         "이동해도 되는지" 확인할 때 부름.
    GET /period-status  지금 그 스윕이 어느 단계인지({"stage":...,
                         "done":...}) — 로딩 화면이 1~2초 간격으로
                         폴링해서 "국가법령정보 API 확인 중 42/102건" 같은
                         실제 단계 문구를 보여주는 데 씀.

왜 파일이 아니라 DB(law_summary)를 보는가: out/summaries/*.json은
"최신이 뭔지"를 파일명·수정시각으로 추론해야 하는데(계약 파일 하나가
여러 데모 조각으로 쪼개질 수도 있어 모호함), law_summary.batch_date는
그 자체로 "이 배치가 언제 것인가"를 확정해 주는 값이라 더 명확함.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, render_template, request
from markupsafe import Markup, escape

from lawtrack.api.client import LawApiClient
from lawtrack.config import PROJECT_ROOT, load_settings
from lawtrack.db.conn import Database
from lawtrack.db.repo import LawSummaryRepo, VersionRepo, WatchlistRepo
from lawtrack.text.split import leading_marker, split_all
from summarizer.textdiff import diff_segments
from webapp.live import (
    PERIOD_WINDOWS,
    PeriodResult,
    ensure_sweep_started,
    get_period_result,
    get_progress,
)

REPORTS_DIR = PROJECT_ROOT / "out" / "reports"

#: 기간 선택 버튼에 쓰는 사람이 읽는 라벨 — PERIOD_WINDOWS(webapp/live.py)의
#: 일수 매핑과 키를 공유하되, 표시 문구는 웹 레이어 관심사라 여기 둠.
PERIOD_LABELS: dict[str, str] = {"5d": "최근 5일", "2w": "최근 2주", "1m": "최근 1개월"}

#: location_label 은 "제56조의2⑤1.가." 처럼 조/항/호/목이 구분자 없이
#: 붙어 나옴(계약 원본 형식 — src/lawtrack/locate/locator.py 참고).
#: 사람이 읽을 땐 "제56조의2" + "⑤" + "1." + "가."로 눈에 보이는 구획이
#: 있어야 항과 호가 안 헷갈림. summarizer/report/builder.py의 _ARTICLE
#: 정규식과 같은 경계 규칙을 씀 — HWPX 표에서 조문 셀을 세로 병합할 때
#: 쓰는 바로 그 경계임.
_ARTICLE_RE = re.compile(r"^제\d+조(?:의\d+)?")
#: 호/목은 "12의2."(호가지번호)처럼 번호 뒤에 "의N"이 붙는 갈래번호
#: 형태가 있음(실측: 제10조의2②12의2. — 국세기본법). "의N" 부분을
#: 빼먹으면 그 조각만 스타일 없는 맨 텍스트로 남아 어색하게 붙어버림.
_POS_TOKEN_RE = re.compile(r"[①-⑳]|[가-힣](?:의\d+)?\.|\d+(?:의\d+)?\.|\d+\)")


def _format_location(label: str) -> Markup:
    """location_label 을 조/항/호/목 구획마다 span 으로 감쌈.

    색으로 항/호/목을 구분하던 걸 그만두고(구분이 잘 안 와닿는다는
      피드백), 대신 숫자 뒤에 "항"/"호"/"목" 글자를 회색으로 덧붙임
      — "③항", "1호", "가목"처럼. 원래 표기의 "." 같은 구두점은 말로
      풀어 쓰면 어색해서 떼어내고 그 자리에 한글 단위를 넣음.

    "제N조" 없이 "③1."처럼 항/호만 오는 값도 있음(이동 전 위치 —
      같은 조 안에서 옮겨진 경우 조 번호를 다시 안 적음). 그런
      값도 같은 규칙으로 항/호/목만 포맷함.

    실측 발견(삭제 항목 마커 표시 후속): 삭제(DELETED_SKIP)
      라벨은 db/repo.py._to_row()가 이미 "(삭제됨 — 개정 전 ①항 참고)"처럼
      사람이 읽는 문장으로 조립해 둔 값이라, 조/항/호 원본 표기(구두점
      기반)가 아님. 이런 문장을 그대로 토큰 스캔에 태우면 "①"이 다시
      항/호/목 패턴으로 잡혀 "①항"의 "항"을 또 붙여 "①항항 참고"처럼
      중복 표기가 생김(항 기호는 뒤에 구두점이 없어도 매칭되는 유일한
      토큰이라 이 케이스만 겉으로 드러났음 — 호/목은 원래 표기에 마침표가
      없어 우연히 안 걸렸을 뿐, 근본 원인은 같음). 실제 location_label
      (SearchUnit에서 나온 원본 표기)은 "제"로 시작하거나 항/호/목 기호로
      바로 시작해 괄호가 올 일이 없으므로, "("로 시작하면 이미 완성된
      문장으로 보고 토큰 스캔 없이 그대로 이스케이프만 해서 돌려줌.
    """
    label = label or ""
    if label.startswith("("):
        return Markup(escape(label))
    m = _ARTICLE_RE.match(label)
    if m:
        article, rest = m.group(0), label[m.end():]
        out = [f'<span class="loc-article">{escape(article)}</span>']
    else:
        rest = label
        out = []
    pos = 0
    for tm in _POS_TOKEN_RE.finditer(rest):
        if tm.start() > pos:
            out.append(str(escape(rest[pos:tm.start()])))
        tok = tm.group(0)
        if re.fullmatch(r"[①-⑳]", tok):
            cls, suffix, num = "loc-clause", "항", tok  # 항 — ①②③
        elif re.fullmatch(r"[가-힣](?:의\d+)?\.", tok):
            cls, suffix, num = "loc-subitem", "목", tok[:-1]  # 목 — 가.나.다. / 가의2.
        else:
            cls, suffix, num = "loc-item", "호", tok.rstrip(".)")  # 호 — 1.2.3. / 12의2. / 1)2)
        out.append(
            f'<span class="{cls}">{escape(num)}<span class="loc-suffix">{suffix}</span></span>'
        )
        pos = tm.end()
    if pos < len(rest):
        out.append(str(escape(rest[pos:])))
    return Markup("".join(out))


def _readable_text(text: str) -> Markup:
    """긴 old_text/new_text를 항/호/목 경계마다 줄바꿈해서 읽기 쉽게 만들고,
    각 줄 맨 앞의 항/호/목 기호만 굵게 강조함.

    실측 발견(지능정보화 기본법 삭제 항목):
    신구법 비교 API는 조문 전체(①~⑦, 그 안의 호까지)를 <P> 블록 하나에
    통짜로 이어붙여 줌 — "원문 보기"에 그대로 뿌리면 줄바꿈 하나 없는
    벽 같은 텍스트가 되어 어디부터 어디까지가 몇 항인지 구분이 안 됨.
    새 정규식을 만드는 대신, 이미 검색에 쓰며(locate/locator.py) 소수점ㆍ
    날짜ㆍ괄호 참조 등 숱한 실측 오탐을 걸러내도록 다듬어진
    text.split.split_all()을 그대로 재사용해 조각 경계마다 줄바꿈만
    넣음.

    실측 후속("7. 영유아의 인권 보호에 관한 업무" 사용자
    리포트): 줄바꿈만으론 부족함 — 줄 맨 앞의 "7." 같은 기호 자체가
    눈에 안 띄어 어디부터 새 항목인지 여전히 헷갈린다는 지적. 각 조각의
    raw 텍스트에 leading_marker()(삭제 항목 라벨에 이미 쓰던 것과 같은
    함수)를 다시 적용해 맨 앞 기호만 <strong>으로 감쌈 — split_all()이
    준 Fragment.marker를 바로 못 쓰는 이유는, 호 목록 앞 전제문처럼 마커가
    Fragment 객체가 아니라 raw 문자열에만 재구성돼 붙는 경우가 있어서임
    (split_all()의 전제문 처리 참고) — raw에서 직접 다시 찾으면 그 경우도
    함께 잡힘.
    """
    text = text or ""
    frags = split_all(text)
    if not frags:
        return Markup(escape(text))
    lines = []
    for f in frags:
        hint = leading_marker(f.raw)
        if hint is not None:
            rest = f.raw[len(hint.marker):]
            lines.append(f"<strong>{escape(hint.marker)}</strong>{escape(rest)}")
        else:
            lines.append(str(escape(f.raw)))
    return Markup("\n".join(lines))


#: "원문 보기"에서 개정 전/후 중 바뀐 어절만 굵게 강조하는 데 씀.
#: summarizer.textdiff.diff_segments()는 이미 apply_mappings()의
#: content_ratio 판정·triage 선별에 쓰이는 같은 어절 단위 diff라,
#: 강조용으로 새 diff 로직을 또 만들지 않고 그대로 재사용함.
def _diff_html(old_text: str, new_text: str, *, side: str) -> Markup:
    """실측 발견("5. 금고 이상의 실형을…"에
    선행 기호 볼드가 안 먹음): 항/호/목 선행 기호를 굵게 하는 처리는
    readable_text() 필터에만 있었고, 이 함수(성공적으로 매칭된 항목의
    "개정 전/후" diff 강조 경로)는 완전히 별도 코드라 그 처리가 안 됐음.
    old_text/new_text 양쪽의 선행 기호가 같으면(제자리 개정이면 보통
    같음 — 항/호 번호 자체는 안 바뀌고 내용만 바뀜) 그 기호를 diff 대상
    에서 떼어내 먼저 굵게 낸 뒤, 나머지만 어절 diff에 넘김. 기호가
    없거나 양쪽이 다르면(번호 자체가 바뀐 드문 경우) 잘못 추측해서
    엉뚱한 걸 굵게 하지 않도록 기존처럼 그대로 둠.
    """
    old_text = old_text or ""
    new_text = new_text or ""
    old_hint = leading_marker(old_text)
    new_hint = leading_marker(new_text)
    prefix = ""
    if old_hint is not None and new_hint is not None and old_hint.marker == new_hint.marker:
        marker = old_hint.marker
        prefix = f"<strong>{escape(marker)}</strong>"
        old_text = old_text[len(marker):]
        new_text = new_text[len(marker):]

    left, right = diff_segments(old_text, new_text)
    changed_kind = "del" if side == "old" else "ins"
    out = [prefix] if prefix else []
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


#: summarizer/report/builder.py의 _TAG와 같은 축약 규칙임. 그대로
#: import하지 않는 이유: builder.py는 무거운 hwpx 패키지를 최상단에서
#: import해서, 가벼워야 할 웹페이지 렌더링에 불필요한 의존성이 끌려옴.
#: HWPX 보고서와 웹페이지가 같은 용어를 쓰도록 값만 그대로 복제함 —
#: 둘 중 하나를 고치면 반드시 다른 쪽도 맞춰야 함.
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

    unit.no_change 를 change_type 보다 먼저 봄: 예를 들어 같은 조문의
    다른 항만 바뀌어 change_type 은 "개정"으로 남아있어도, 이 항목 자체는
    안 바뀐 경우(no_change=True)엔 "변경없음"으로 보여야 함 — HWPX
    보고서와 웹페이지가 같은 데이터를 다르게 보여주면 안 되므로 규칙을
    그대로 복제했음.
    """
    unit = article_summary.get("unit", {})
    if unit.get("no_change"):
        return "변경없음"
    change_type = unit.get("change_type", "")
    return _TAG.get(change_type, change_type)


#: kind_of()가 돌려주는 한글 라벨 → 색 클래스. 통계 타일·법령 개요 점·
#: 구분 섹션 제목·필터 칩이 전부 이 매핑 하나를 공유함.
_KIND_CSS = {
    "신설": "new",
    "개정": "amend",
    "삭제": "delete",
    "이동": "move",
    "이동개정": "move",
    "변경": "other",
    "변경없음": "nochange",
}


def _kind_css_for_label(kind_label: str) -> str:
    """통계 배지처럼 article_summary dict 없이 라벨(예: '신설')만 있을 때."""
    return _KIND_CSS.get(kind_label, "other")


#: 법령 카드 왼쪽 강조선 색을 고를 때, 여러 구분이 섞여 있으면 어느 걸
#: 대표색으로 쓸지 우선순위. "신설"이 담당자에게 가장 중요한 신호라
#: 맨 앞에 둠 — 예를 들어 조문 10개 중 1개만 신설이어도 그 법령 카드는
#: 신설색으로 강조되어야 눈에 띔(다수결로 고르면 묻힘).
_KIND_PRIORITY = ["신설", "삭제", "이동", "이동개정", "개정", "변경", "변경없음"]


def _law_dominant_kind(law: dict) -> str:
    """법령 카드 왼쪽 강조선에 쓸 대표 구분 하나를 고름."""
    kinds = {_kind_of(a) for a in law.get("article_summaries", [])}
    for k in _KIND_PRIORITY:
        if k in kinds:
            return k
    return "변경"


#: law.source_url(DB) 은 src/lawtrack/contract/export.py의 _source_url() 이
#: 만든 DRF(Data Reference API) 링크(?target=law&MST=...)다 — OC 인증키를
#: 일부러 뺐으므로(export.py 주석 참고, 키 유출 방지) 그 자체로는 열리지
#: 않음. law.go.kr은 인증키 없이도 "/법령/{법령명}", "/행정규칙/{명}"
#: 같은 이름 기반 공개 URL을 지원함(실측: 전자정부법/전자정부법 시행령/
#: 행정규칙 이름 모두 200 확인) — 웹페이지에선 이걸 대신 씀.
def _public_law_url(law_type: str, law_name: str) -> str:
    prefix = "행정규칙" if law_type == "행정규칙" else "법령"
    return f"https://www.law.go.kr/{prefix}/{quote(law_name)}"


#: _group_by_kind()가 만드는 섹션 순서 — _summary_stats()의 order와
#: 반드시 같아야 통계 타일 순서와 본문 섹션 순서가 어긋나지 않음.
_KIND_ORDER = ["개정", "신설", "삭제", "이동", "이동개정", "변경없음", "변경"]


def _group_by_kind(laws: list[dict]) -> list[dict]:
    """조문 요약을 "구분"(신설/개정/삭제/...) 별로 묶은 평평한 목록으로
    바꿈.

    설계: 예전엔 법령 카드 하나 안에 그
    법령의 조문들이 다 들어있는 구조였는데("법령 → 조문"), 그러다 보니
    카드 하나에 구분이 여러 개 섞여(신설 1건 + 개정 9건 등) 카드 자체를
    무슨 색으로 칠해야 할지 계속 애매했음(왼쪽 띠 → 컬러 섀도 다 시도해도
    "티가 안 난다"는 피드백). 근본 원인은 색이 조문 하나하나의 속성인데
    법령 단위 컨테이너에 억지로 대표색을 씌우려 한 것 — 그래서 아예
    "구분 → 법령+조문" 으로 순회 축을 뒤집음. 색은 이제 섹션 제목
    하나에만 있으면 되고, 그 밑의 개별 항목은 "이 섹션 안에 있다"는
    사실 자체로 이미 구분이 확정되므로 항목마다 색을 또 표시할 필요가
    없어짐.
    """
    buckets: dict[str, list[dict]] = {}
    for law in laws:
        for a in law.get("article_summaries", []):
            kind = _kind_of(a)
            buckets.setdefault(kind, []).append({
                "law_name": law.get("law_name", ""),
                "law_type": law.get("law_type", ""),
                "enforce_date": law.get("enforce_date"),
                "article": a,
            })
    ordered_kinds = [k for k in _KIND_ORDER if k in buckets]
    ordered_kinds += [k for k in buckets if k not in _KIND_ORDER]
    return [
        {"kind": kind, "css": _kind_css_for_label(kind), "entries": buckets[kind]}
        for kind in ordered_kinds
    ]


def _summary_stats(laws: list[dict]) -> dict:
    """헤더 상단 통계 배지용 — 구분별 조문 건수 + 총계.

    HWPX 보고서엔 없는, 웹페이지라서 가능한 "한눈에 보기" 요약임.
    """
    counts: dict[str, int] = {}
    total = 0
    for law in laws:
        for a in law.get("article_summaries", []):
            kind = _kind_of(a)
            counts[kind] = counts.get(kind, 0) + 1
            total += 1
    breakdown = [(k, counts[k]) for k in _KIND_ORDER if k in counts]
    breakdown += [(k, v) for k, v in counts.items() if k not in _KIND_ORDER]
    return {"total_laws": len(laws), "total_articles": total, "breakdown": breakdown}


def _period_context(result: PeriodResult) -> dict:
    laws = result.laws
    return {
        "batch_date": None,
        "laws": laws,
        "stats": _summary_stats(laws),
        "sections": _group_by_kind(laws),
        "period": result.window_key,
        "period_label": PERIOD_LABELS.get(result.window_key, result.window_key),
        "period_from": result.from_date,
        "period_to": result.to_date,
        "period_errors": result.errors,
        "period_windows": PERIOD_LABELS,
    }


def create_app(
    repo: LawSummaryRepo | None = None,
    live_check: Callable[[str], PeriodResult] | None = None,
    *,
    enable_background_refresh: bool = False,
    sweep_starter: Callable[[str], bool] | None = None,
    progress_getter: Callable[[str], dict] | None = None,
    watchlist_repo: WatchlistRepo | None = None,
    version_repo: VersionRepo | None = None,
    law_api_client_factory: Callable[[], LawApiClient] | None = None,
    revision_lookup: Callable[[list[str]], dict[str, dict]] | None = None,
) -> Flask:
    """앱 팩토리. repo/live_check/sweep_starter/progress_getter를 주입할
    수 있어 테스트에서 진짜 DB나 실 API+LLM 없이 확인 가능함.

    enable_background_refresh 기본값이 False인 이유: 켜면 실제로
    국가법령정보 API를 도는 백그라운드 스레드가 시작됨(webapp/live.py의
    start_background_refresh) — pytest 등 테스트에서 앱을 만들 때마다
    이게 켜지면 안 되므로, 실 서버 진입점(맨 아래 __main__)에서만
    명시적으로 켬.
    """
    app = Flask(__name__)
    app.jinja_env.globals["kind_css_for_label"] = _kind_css_for_label
    app.jinja_env.globals["law_dominant_kind"] = _law_dominant_kind
    app.jinja_env.globals["format_location"] = _format_location
    app.jinja_env.globals["public_law_url"] = _public_law_url
    app.jinja_env.globals["diff_old_html"] = _diff_old_html
    app.jinja_env.globals["diff_new_html"] = _diff_new_html
    app.jinja_env.filters["readable_text"] = _readable_text
    app.jinja_env.globals["period_windows"] = PERIOD_LABELS
    # 지연 생성. 예전에는 여기서 곧바로 _build_repo() 를 불렀는데, 그러면
    #   create_app() 만 해도 .env(LAW_API_OC·POSTGRES_PASSWORD)가 있어야 함.
    #   배포 꾸러미를 막 푼 상태(=.env 를 아직 안 채운 상태)에서 pytest 를
    #   돌리면 요약 라우트와 무관한 테스트 18건이 ConfigError 로 죽었음.
    #   아래 register_law_routes 의 3개 인자와 같은 방식으로 미룸 —
    #   /summary 를 실제로 열 때 처음 만듦.
    _repo_cache: list[LawSummaryRepo] = []

    def _repo_of() -> LawSummaryRepo:
        if repo is not None:
            return repo
        if not _repo_cache:
            _repo_cache.append(_build_repo())
        return _repo_cache[0]

    _live_check = live_check or (lambda window_key: get_period_result(load_settings(), window_key))
    _sweep_starter = sweep_starter or (lambda window_key: ensure_sweep_started(load_settings(), window_key))
    _progress_getter = progress_getter or get_progress

    if enable_background_refresh:
        from webapp.live import start_background_refresh

        start_background_refresh(load_settings())

    from webapp.laws import register_law_routes

    # watchlist_repo/version_repo/law_api_client_factory를 안 넘기면
    # register_law_routes 내부에서 처음 /laws 요청이 올 때야 비로소 실
    # Database/LawApiClient를 만듦(지연 생성) — _repo(위)와 달리 이
    # 3개는 create_app() 호출 시점에 곧바로 연결을 열지 않음. 기존
    # 테스트 대부분이 이 새 기능과 무관한데도 create_app()을 부를 때마다
    # 실 DB 커넥션 풀이 추가로 열리는 걸 막기 위해서임.
    register_law_routes(
        app, watchlist_repo=watchlist_repo, version_repo=version_repo,
        client_factory=law_api_client_factory, revision_lookup=revision_lookup,
    )

    # PDF/HWPX 업로드 → 감시 대상 인용 확인. 매칭 자체는 DB·API·LLM
    # 미사용(사전은 seed 파일에서 지연 생성)이고, 개정 배지·요약 한 줄만
    # DB에서 부가로 읽음 — 조회 실패 시 배지 없이 매칭 결과만 냄.
    # revision_lookup 은 /laws 상세의 현재 열 배지와 공유함(같은 정보).
    from webapp.pdfcheck import register_pdf_routes

    register_pdf_routes(app, revision_lookup=revision_lookup)

    def _validate_period(period: str | None) -> None:
        if period is not None and period not in PERIOD_WINDOWS:
            abort(400, f"알 수 없는 기간입니다: {period!r} (허용: {list(PERIOD_WINDOWS)})")

    @app.get("/period-check")
    def period_check() -> Response:
        period = request.args.get("period")
        _validate_period(period)
        if period is None:
            abort(400, "period 파라미터가 필요합니다.")
        needs_wait = _sweep_starter(period)
        return jsonify({"ready": not needs_wait})

    @app.get("/period-status")
    def period_status() -> Response:
        period = request.args.get("period")
        _validate_period(period)
        if period is None:
            abort(400, "period 파라미터가 필요합니다.")
        progress = _progress_getter(period)
        done = progress.get("stage") == "완료"
        return jsonify({
            "stage": progress.get("stage", ""), "detail": progress.get("detail", ""), "done": done,
        })

    @app.get("/")
    def home() -> str:
        """세 기능(개정 요약/전문 보기/PDF 업로드) 중 하나를 고르는 허브.

        설계: 원래 "/"가 곧 개정 요약
        화면(report.html)이었는데, 전문 비교(/laws)에 이어 PDF 업로드
        기능까지 세 번째로 예정되면서 "메인에서 셋 중 하나를 고른다"는
        구조로 바꾸기로 했음. 기존 "/"를 그대로 개정 요약에 물려 두면
        허브가 들어설 자리가 없으므로, 개정 요약을 /summary로 옮기고
        (endpoint 이름은 index로 그대로 둠 — url_for('index')를 쓰는
        기존 템플릿들이 안 바뀌어도 되게) "/"는 이 허브 전용으로 비움.
        PDF 업로드는 다른 담당자가 구현할 기능이라 여기서는 "준비중"
        비활성 카드로만 자리를 잡아 둠.
        """
        return render_template("home.html")

    @app.get("/summary")
    def index() -> str:
        period = request.args.get("period")
        if period:
            _validate_period(period)
            result = _live_check(period)
            return render_template("report.html", **_period_context(result))

        batch_date = _repo_of().latest_batch_date()
        if batch_date is None:
            return render_template(
                "report.html", batch_date=None, laws=[], stats=None, sections=[],
                period=None, period_windows=PERIOD_LABELS,
            )
        laws = _repo_of().fetch_by_batch(batch_date)
        stats = _summary_stats(laws)
        sections = _group_by_kind(laws)
        return render_template(
            "report.html", batch_date=batch_date, laws=laws, stats=stats, sections=sections,
            period=None, period_windows=PERIOD_LABELS,
        )

    @app.get("/download")
    def download() -> Response:
        from flask import send_file

        period = request.args.get("period")
        if period:
            _validate_period(period)
            result = _live_check(period)
            if result.hwpx_path is None or not result.hwpx_path.exists():
                abort(404, "이 기간에는 변경사항이 없어 다운로드할 보고서가 없습니다.")
            return send_file(
                result.hwpx_path, as_attachment=True, download_name=result.hwpx_path.name,
                mimetype="application/octet-stream",
            )

        batch_date = _repo_of().latest_batch_date()
        if batch_date is None:
            abort(404, "아직 생성된 배치가 없습니다.")
        laws = _repo_of().fetch_by_batch(batch_date)
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
    # threaded=True: 기간 즉석 조회가 폴링(/period-check, /period-status)을
    # 쓰므로, 페이지 이동 요청 하나가 서버를 붙들고 있는 동안에도 그
    # 폴링 요청들이 동시에 처리돼야 함 — 기본값(단일 스레드)이면 앞
    # 요청이 끝나야 다음 요청을 받아서, 폴링이 실제로 막힐 수 있음.
    #
    # debug 는 기본이 꺼짐임. Flask 의 디버그 화면은 브라우저에서
    #   임의 코드를 실행할 수 있는 콘솔을 열어 주므로, 원내에 올린 채로
    #   켜 두면 안 됨. 개발 중에만 WEB_DEBUG=1 로 켬.
    #
    # WEB_HOST 기본값이 127.0.0.1 인 이유: 같은 PC 에서만 열림. 다른
    # 자리에서도 접속하게 하려면 WEB_HOST=0.0.0.0 으로 켜되, 사내망
    # 방화벽 정책을 먼저 확인할 것.
    create_app(enable_background_refresh=True).run(
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "5000")),
        debug=os.environ.get("WEB_DEBUG") == "1",
        threaded=True,
        use_reloader=False,
    )
