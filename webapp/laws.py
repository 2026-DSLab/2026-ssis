"""전문(全文) 비교 페이지 — 감시 대상 102건 목록 + 법 하나 클릭 시
개정 전/개정 후(필요하면 전전까지) 전문을 나란히 보여준다.

라우트:
    GET /laws                 감시 대상 목록(watchlist 전체).
    GET /laws/<law_id>        전문 비교. ?depth=3 이면 전전 버전까지.

기존 "/" (주간 배치 요약) 과는 완전히 다른 관심사라 별도 파일로 뺐다 —
webapp/app.py 는 law_summary(요약)만 보는 반면, 여기는 documents(원문
전문)를 다루고 필요하면 그 자리에서 실 API를 부른다.

★ 처음 보는 법을 열면 실 API가 최대 4번(oldAndNew+fulltext, depth=3이면
그 두 배) 불릴 수 있다 — 그래도 "최근 5일/2주/1개월" 스윕(watchlist
102건 전체를 도는 것)과 달리 법 하나뿐이라 수 초 안에 끝난다. 그래서
그쪽처럼 로딩 화면+폴링을 따로 만들지 않고 그냥 페이지 요청 안에서
동기로 처리한다. 한 번 받아온 버전은 documents에 캐싱되므로 같은 법을
다시 열면 그 뒤로는 즉시 뜬다.
"""

from __future__ import annotations

from typing import Callable

from flask import Flask, abort, render_template, request

from lawtrack.api.client import LawApiClient
from lawtrack.db.repo import VersionRepo, WatchlistRepo
from lawtrack.history import build_version_chain, kind_of
from lawtrack.parse.fulltext import searchable_units_for

#: 상세 페이지가 허용하는 비교 단수. 그 이상은 UI에 노출하지 않는다
#: (사용자 결정 2026-08-05: 기본 2단 + "전전 버전 보기" 버튼으로 3단).
_MIN_DEPTH = 2
_MAX_DEPTH = 3

#: build_version_chain()은 오래된→최신 순으로 돌려주므로, "현재 버전과
#: 몇 단계 떨어졌는가"(0=현재)로 라벨을 정한다 — depth가 2든 3이든
#: 맨 뒤(distance=0)가 항상 "현재"라는 사실은 안 변하므로 이 방식이
#: loop.index로 직접 분기하는 것보다 안전하다(3단에서 맨 앞을 "개정
#: 전"으로 잘못 라벨링하는 실수를 방지).
_LABELS_BY_DISTANCE = {0: "개정 후 (현재)", 1: "개정 전", 2: "개정 전전"}


def _format_yyyymmdd(s: str) -> str:
    """"20260516" -> "2026-05-16". 형식이 아니면(빈 값 등) 원본 그대로."""
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _group_by_article(units: list) -> list[dict]:
    """SearchUnit 목록(문서 순서 보존)을 조문 단위로 묶는다.

    flatten_searchable()/parse_admrul_units() 둘 다 같은 조문에 속한
    유닛을 연달아 내놓으므로, article_label이 바뀔 때만 새 그룹을
    시작하면 된다(HWPX 표의 조문 열 세로병합과 같은 전제).

    각 줄에는 html(강조 없는 기본 렌더링)과 status="same"을 미리 채워
    둔다 — _apply_diff_highlight()가 실제로 비교 가능한 열 쌍에서만
    이 값을 덮어쓰므로, 템플릿은 항상 line.html 하나만 보면 된다.

    ★ 실측 발견(2026-08-05, 사용자 리포트 — "<개정 2003.12.31>"이
    화면에 그대로 뜬다): article_diff/law_summary로 가는 기존 경로는
    old_text/new_text를 저장하기 직전에 strip_annotations를 이미
    적용한다(repo.py — "<개정 2014.1.10.> 같은 각주가 LLM팀에게 그대로
    노출되고 있었다"는 같은 종류의 버그를 2026-07-16에 이미 한 번 고친
    이력이 있다). parse_admrul_units()는 그 처리를 이미 내장하고 있지만
    parse_articles()/flatten_searchable()(법령 경로)는 검색용 원문을
    그대로 돌려주므로 이 각주가 안 걸러진 채 남는다 — 여기서 한 번 더
    걸러 기존 개정 요약 페이지와 같은 "각주 없는" 화면을 보장한다
    (admrul 쪽은 이미 제거된 뒤라 다시 걸러도 아무 변화 없음 — 안전).
    """
    from lawtrack.text.split import strip_annotations
    from webapp.app import _readable_text

    groups: list[dict] = []
    current = None
    for u in units:
        if current is None or current["article_label"] != u.article_label:
            current = {"article_label": u.article_label, "lines": []}
            groups.append(current)
        text = strip_annotations(u.text or "").strip()
        if text:
            current["lines"].append({
                "location_label": u.location_label, "text": text,
                "html": _readable_text(text), "status": "same",
            })
    return groups


def _apply_diff_highlight(old_col: dict, new_col: dict) -> None:
    """old_col(개정 전 쪽) / new_col(개정 후 쪽)의 줄에 색깔 강조를 입힌다.

    ★ 실측(2026-08-05, 사용자 요청 — 기존 개정 요약 페이지의 "개정 전/
    개정 후" 강조를 전문 비교 페이지에도 달라는 요청): 이미 report.html이
    쓰는 _diff_old_html/_diff_new_html(어절 단위 diff, summarizer.textdiff
    재사용)와 완전히 같은 색·마크업을 그대로 재사용한다 — 화면마다 강조
    방식이 다르면 "이 색이 그 색이다"를 매번 다시 익혀야 한다.

    위치 대응은 location_label을 그대로 키로 써서 짝짓는다 — 계약
    파이프라인의 6-가드 매칭 엔진(locate.py)만큼 정교하지 않아 조문
    번호가 옮겨간 경우("이동")는 삭제+신설처럼 보일 수 있지만, 이 화면은
    "전문 자체를 보여주는" 용도라 그 정도 단순화는 감수한다(정교한
    이동 판정은 개정 요약 페이지의 몫으로 남겨 둔다).
    """
    from webapp.app import _diff_new_html, _diff_old_html, _readable_text

    old_map = {ln["location_label"]: ln for art in old_col["articles"] for ln in art["lines"]}
    new_map = {ln["location_label"]: ln for art in new_col["articles"] for ln in art["lines"]}

    for loc, old_line in old_map.items():
        new_line = new_map.get(loc)
        if new_line is None:
            old_line["status"] = "removed"
        elif new_line["text"] != old_line["text"]:
            old_line["status"] = "changed"
            old_line["html"] = _diff_old_html(old_line["text"], new_line["text"])
            new_line["status"] = "changed"
            new_line["html"] = _diff_new_html(old_line["text"], new_line["text"])

    for loc, new_line in new_map.items():
        if loc not in old_map:
            new_line["status"] = "added"


def register_law_routes(
    app: Flask, *,
    watchlist_repo: WatchlistRepo | None = None,
    version_repo: VersionRepo | None = None,
    client_factory: Callable[[], LawApiClient] | None = None,
) -> None:
    """라우트 2개를 등록한다. 세 의존성 모두 안 넘기면(기본값 None) 실
    Database/LawApiClient는 처음 /laws 요청이 올 때야 만든다(지연 생성)
    — create_app()을 부르기만 해도 곧바로 실 DB 커넥션이 열리는 걸
    막는다(이 페이지와 무관한 대부분의 웹앱 테스트가 매번 그 비용을
    치르지 않도록).
    """
    _lazy: dict[str, object] = {}

    def _get_watchlist_repo() -> WatchlistRepo:
        if watchlist_repo is not None:
            return watchlist_repo
        if "watchlist_repo" not in _lazy:
            from lawtrack.config import load_settings
            from lawtrack.db.conn import Database

            _lazy["watchlist_repo"] = WatchlistRepo(Database(load_settings().db))
        return _lazy["watchlist_repo"]

    def _get_version_repo() -> VersionRepo:
        if version_repo is not None:
            return version_repo
        if "version_repo" not in _lazy:
            from lawtrack.config import load_settings
            from lawtrack.db.conn import Database

            _lazy["version_repo"] = VersionRepo(Database(load_settings().db))
        return _lazy["version_repo"]

    def _make_client() -> LawApiClient:
        if client_factory is not None:
            return client_factory()
        from lawtrack.config import load_settings

        return LawApiClient(load_settings().api)

    @app.get("/laws")
    def laws_list() -> str:
        entries = sorted(_get_watchlist_repo().active(), key=lambda e: e.official_name)
        kinds = sorted({e.law_type for e in entries})
        return render_template("laws_list.html", entries=entries, kinds=kinds)

    @app.get("/laws/<law_id>")
    def law_detail(law_id: str) -> str:
        entry = _get_watchlist_repo().get(law_id)
        if entry is None or entry.status != "현행":
            abort(404, f"감시 목록에 없는 법입니다: {law_id!r}")
        if not entry.last_serial_no:
            abort(404, f"{entry.official_name}: 아직 확인된 버전이 없습니다.")

        depth = _MIN_DEPTH
        if request.args.get("depth") == "3":
            depth = _MAX_DEPTH

        kind = kind_of(entry.law_type)
        client = _make_client()
        try:
            chain = build_version_chain(
                client, _get_version_repo(), kind=kind, doc_id=entry.law_id,
                doc_name=entry.official_name, current_serial=entry.last_serial_no,
                depth=depth,
            )
        finally:
            client.close()

        n = len(chain)
        columns = [
            {
                "serial_no": v.serial_no,
                # ★ 사용자 요청(2026-08-05): "2100000272436" 같은 일련번호
                # 대신 시행일/공포일을 보여달라는 요청 — 시행일이 없으면
                # 공포일로, 그것도 없으면(드묾) 일련번호로 최종 폴백한다.
                "date_label": (
                    _format_yyyymmdd(v.enforce_date)
                    or _format_yyyymmdd(v.promulgation_date)
                    or v.serial_no
                ),
                "is_current": v.serial_no == entry.last_serial_no,
                "label": _LABELS_BY_DISTANCE.get((n - 1) - i, f"{(n - 1) - i}단계 전"),
                "articles": _group_by_article(searchable_units_for(kind, v.full_text)),
                "diff_role": "",
            }
            for i, v in enumerate(chain)
        ]
        # 가장 최근 전환(직전 버전 -> 현재 버전)만 강조한다 — depth=3이라
        # 전전 버전까지 있어도, 맨 왼쪽(전전) 열은 그냥 참고용 원문으로
        # 둔다(가운데 열이 "전전과 비교한 강조"와 "후와 비교한 강조"를
        # 동시에 띠면 오히려 헷갈린다).
        if len(columns) >= 2:
            _apply_diff_highlight(columns[-2], columns[-1])
            # report.html의 mark.diff-changed 색 규칙(fulltext-col--old=
            # 빨강, fulltext-col--new=파랑)이 그대로 먹도록, 강조 대상인
            # 마지막 두 열에만 같은 클래스를 붙인다 — 새 색 규칙을 또
            # 만들지 않고 기존 개정 요약 페이지와 완전히 같은 색을 쓴다.
            columns[-2]["diff_role"] = "old"
            columns[-1]["diff_role"] = "new"
        # depth=3을 요청했는데 체인이 그만큼 안 나왔다 = 최초 제정본까지
        # 거슬러 올라가 더 이전 버전이 없다는 뜻(오류 아님) — 화면에
        # 그 사실을 알려준다. depth=2일 땐 "전전 버전 보기" 버튼을 그냥
        # 항상 보여준다(눌러봐야 있는지 없는지 아니까).
        no_earlier_version = depth == _MAX_DEPTH and len(chain) < depth
        show_expand_button = depth < _MAX_DEPTH

        return render_template(
            "law_detail.html", entry=entry, columns=columns, depth=depth,
            show_expand_button=show_expand_button, no_earlier_version=no_earlier_version,
        )
