"""전문(全文) 비교 페이지 — 감시 대상 102건 목록 + 법 하나 클릭 시
개정 전/개정 후(필요하면 전전까지) 전문을 나란히 보여줌.

라우트:
    GET /laws                 감시 대상 목록(watchlist 전체).
    GET /laws/<law_id>        전문 비교. ?depth=3 이면 전전 버전까지.

기존 "/" (주간 배치 요약) 과는 완전히 다른 관심사라 별도 파일로 뺐음 —
webapp/app.py 는 law_summary(요약)만 보는 반면, 여기는 documents(원문
전문)를 다루고 필요하면 그 자리에서 실 API를 부름.

처음 보는 법을 열면 실 API가 최대 4번(oldAndNew+fulltext, depth=3이면
그 두 배) 불릴 수 있음 — 그래도 "최근 5일/2주/1개월" 스윕(watchlist
102건 전체를 도는 것)과 달리 법 하나뿐이라 수 초 안에 끝남. 그래서
그쪽처럼 로딩 화면+폴링을 따로 만들지 않고 그냥 페이지 요청 안에서
동기로 처리함. 한 번 받아온 버전은 documents에 캐싱되므로 같은 법을
다시 열면 그 뒤로는 즉시 뜸.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from flask import Flask, abort, render_template, request

from lawtrack.api.client import LawApiClient, LawApiError
from lawtrack.db.repo import VersionRepo, WatchlistRepo
from lawtrack.history import build_version_chain, kind_of
from lawtrack.parse.fulltext import searchable_units_for

log = logging.getLogger(__name__)

#: 상세 페이지가 허용하는 비교 단수. 그 이상은 UI에 노출하지 않음
#: (기본 2단 + "전전 버전 보기" 버튼으로 3단).
_MIN_DEPTH = 2
_MAX_DEPTH = 3

#: build_version_chain()은 오래된→최신 순으로 돌려주므로, "현재 버전과
#: 몇 단계 떨어졌는가"(0=현재)로 라벨을 정한다 — depth가 2든 3이든
#: 맨 뒤(distance=0)가 항상 "현재"라는 사실은 안 변하므로 이 방식이
#: loop.index로 직접 분기하는 것보다 안전함(3단에서 맨 앞을 "개정
#: 전"으로 잘못 라벨링하는 실수를 방지).
_LABELS_BY_DISTANCE = {0: "개정 후 (현재)", 1: "개정 전", 2: "개정 전전"}


def _format_yyyymmdd(s: str) -> str:
    """"20260516" -> "2026-05-16". 형식이 아니면(빈 값 등) 원본 그대로."""
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _date_of(version) -> dict:
    """버전 하나의 머리에 적을 날짜와 그 종류.

    시행일을 먼저 쓰고, 없으면 공포일, 그것도 없으면(드묾) 일련번호로 내려감.
    date_kind 가 비면 화면은 날짜 없이 일련번호만 보여 줌.
    """
    enforce = _format_yyyymmdd(version.enforce_date)
    if enforce:
        return {"date_label": enforce, "date_kind": "시행"}
    promulgated = _format_yyyymmdd(version.promulgation_date)
    if promulgated:
        return {"date_label": promulgated, "date_kind": "공포"}
    return {"date_label": version.serial_no, "date_kind": ""}


def _group_by_article(units: list) -> list[dict]:
    """SearchUnit 목록(문서 순서 보존)을 조문 단위로 묶음.

    flatten_searchable()/parse_admrul_units() 둘 다 같은 조문에 속한
    유닛을 연달아 내놓으므로, article_label이 바뀔 때만 새 그룹을
    시작하면 됨(HWPX 표의 조문 열 세로병합과 같은 전제).

    각 줄에는 html(강조 없는 기본 렌더링)과 status="same"을 미리 채워
    둠 — _apply_diff_highlight()가 실제로 비교 가능한 열 쌍에서만
    이 값을 덮어쓰므로, 템플릿은 항상 line.html 하나만 보면 됨.

    실측 발견("<개정 2003.12.31>"이
    화면에 그대로 뜸): article_diff/law_summary로 가는 기존 경로는
    old_text/new_text를 저장하기 직전에 strip_annotations를 이미
    적용함(repo.py — "<개정 2014.1.10.> 같은 각주가 요약 단계에 그대로
    노출되고 있었다"는 같은 종류의 버그를 이미 한 번 고친 이력이 있다). parse_admrul_units()는 그 처리를 이미 내장하고 있지만
    parse_articles()/flatten_searchable()(법령 경로)는 검색용 원문을
    그대로 돌려주므로 이 각주가 안 걸러진 채 남음 — 여기서 한 번 더
    걸러 기존 개정 요약 페이지와 같은 "각주 없는" 화면을 보장함
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


#: 라벨 끝의 마침표/공백. 같은 항목인데 버전마다 "1의2." / "1의2" 로
#: 표기가 갈려서(아래 _match_key 주석 참고) 짝짓기에 실패하는 걸 막음.
_LABEL_TAIL_RE = re.compile(r"[.\s·ㆍ]+$")


def _match_key(location_label: str) -> str:
    """두 버전의 같은 줄을 이어 줄 비교용 키.

    실측 버그("변한 게 없는데 색깔 표시가
    돼있다"): 국가를 당사자로 하는 계약에 관한 법률 시행령에서 같은
    항목의 라벨이 버전마다 끝점 유무로 갈렸음.

        개정 전: "제110조②1의2."      개정 후: "제110조②1의2"

    내용은 한 글자도 안 다른데 location_label 이 달라 짝을 못 찾고
    한쪽은 '삭제', 다른 쪽은 '신설'로 표시됐음(이 법 한 건에서만 18줄).
    끝의 마침표·공백만 떼어 같은 항목으로 이어 줌 — 가운데 표기 차이는
    건드리지 않아, 진짜로 다른 항목이 잘못 합쳐질 위험은 없음.
    """
    return _LABEL_TAIL_RE.sub("", location_label or "")


def _index_lines(col: dict) -> dict[str, list[dict]]:
    """비교용 키 -> 그 키를 가진 줄들(문서 순서 그대로).

    실측 버그("변한 게 없는데 색깔 표시가
    돼있다", 국가를 당사자로 하는 계약에 관한 법률 시행령): 원래는
    {location_label: line} 딕셔너리로 만들었는데, location_label 은
    고유하지 않음 — 실측으로 이 시행령 한 건에서만 5종이 중복이었음
    (예: "제1조"가 장 제목 "제1장 총칙" 줄과 실제 본문 줄 양쪽에 붙음.
    SearchUnit.location_label 은 조문/항/호/목만 이어 붙이므로 같은 조에
    속한 이런 줄들이 같은 라벨을 갖음).

    딕셔너리로 만들면 뒤 줄이 앞 줄을 조용히 덮어써서, 살아남은 줄이
    상대 열의 엉뚱한 줄과 짝지어짐 — 내용이 똑같은데 "바뀜"으로
    강조되고, 덮어써진 줄은 비교 자체가 누락됨. 그래서 라벨당 하나가
    아니라 목록으로 모아 두고, 호출부가 같은 라벨끼리 순서대로(1번째는
    1번째와, 2번째는 2번째와) 짝짓음.
    """
    idx: dict[str, list[dict]] = {}
    for art in col["articles"]:
        for line in art["lines"]:
            idx.setdefault(_match_key(line["location_label"]), []).append(line)
    return idx


def _apply_diff_highlight(old_col: dict, new_col: dict) -> None:
    """old_col(개정 전 쪽) / new_col(개정 후 쪽)의 줄에 색깔 강조를 입힘.

    실측(기존 개정 요약 페이지의 "개정 전/
    개정 후" 강조를 전문 비교 페이지에도 달라는 요청): 이미 report.html이
    쓰는 _diff_old_html/_diff_new_html(어절 단위 diff, summarizer.textdiff
    재사용)와 완전히 같은 색·마크업을 그대로 재사용함 — 화면마다 강조
    방식이 다르면 "이 색이 그 색이다"를 매번 다시 익혀야 함.

    ── 짝짓기 3단계 ──────────────────────────────────────────────
    이 화면이 답해야 하는 질문은 "무슨 **내용**이 바뀌었나"다. 그래서
    다음을 불변조건으로 삼음:

        글자 하나 안 바뀐 줄에는 절대 색을 칠하지 않음.

    실측 전수검증("이 부분은 변한 게 없는데
    색깔 표시가 돼있어"에서 출발): 라벨만으로 짝지으면 조문 번호가
    옮겨간 경우에 이 불변조건이 깨짐 — 캐시된 19개 문서에서 24줄이
    "내용은 동일한데 삭제/신설/변경"으로 표시됐음.

        개인정보의 안전성 확보조치 기준: 제18조   -> 제19조
        도로교통법:                     제116조1. -> 제116조①1.
        아동복지법:                     제22조⑥1. -> 제22조⑦1.

    아동복지법처럼 항 번호가 통째로 밀리면 라벨 짝짓기가 어긋나면서,
    옮겨간 줄은 '삭제'로, 그 자리를 차지한 다른 줄은 엉뚱한 상대와
    비교돼 '변경'으로 나옴. 그래서 라벨로 한 번 맞춘 뒤, 내용이
    똑같은 줄이 상대 열에 남아 있으면 그쪽으로 다시 잇음.

    옮겨간 사실 자체는 라벨에 이미 보이므로 따로 표시하지 않음
    (정교한 이동 판정은 개정 요약 페이지의 몫으로 남겨 둠).
    """
    from webapp.app import _diff_new_html, _diff_old_html

    old_lines = [ln for art in old_col["articles"] for ln in art["lines"]]
    new_lines = [ln for art in new_col["articles"] for ln in art["lines"]]

    # 1단계 — 라벨이 같은 줄끼리 나온 순서대로. 같은 라벨이 여러 줄이어도
    # (장 제목 줄 + 본문 줄 등) 앞은 앞끼리, 뒤는 뒤끼리 짝지어짐.
    new_by_key: dict[str, list[dict]] = {}
    for line in new_lines:
        new_by_key.setdefault(_match_key(line["location_label"]), []).append(line)

    taken: set[int] = set()
    cursor: dict[str, int] = {}
    pairs: list[tuple[dict, dict | None]] = []
    for line in old_lines:
        key = _match_key(line["location_label"])
        bucket = new_by_key.get(key, [])
        i = cursor.get(key, 0)
        if i < len(bucket):
            partner = bucket[i]
            cursor[key] = i + 1
            taken.add(id(partner))
            pairs.append((line, partner))
        else:
            pairs.append((line, None))

    # 2단계 — 내용이 어긋난 짝은, 상대 열에 "글자까지 똑같은" 줄이 있으면
    # 그쪽으로 갈아탐(조문 번호만 옮겨간 경우를 여기서 되살림).
    #
    # 상대가 이미 다른 줄에 물려 있어도, 그 짝이 어차피 내용이 어긋난
    # 짝이라면 뺏어옴 — 정확히 일치하는 짝이 어긋난 짝보다 언제나 나음.
    # 이미 내용까지 맞는 짝은 절대 건드리지 않음.
    #
    # 뺏기면 원래 임자가 짝을 잃고, 그 임자도 제 짝을 다른 데서 찾아야
    # 할 수 있음(항 번호가 통째로 밀리면 이런 연쇄가 생김 — 아동복지법
    # 제22조⑥→⑦ 실측). 그래서 더 이상 바뀌지 않을 때까지 반복함.
    by_text: dict[str, list[dict]] = {}
    for line in new_lines:
        by_text.setdefault(line["text"], []).append(line)

    owner: dict[int, int] = {id(n): i for i, (_, n) in enumerate(pairs) if n is not None}

    def _exact(i: int) -> bool:
        o, n = pairs[i]
        return n is not None and n["text"] == o["text"]

    for _ in range(len(pairs) + 1):  # 상한만 둔 고정점 반복(보통 1~2회)
        moved = False
        for idx, (old_line, partner) in enumerate(pairs):
            if _exact(idx):
                continue
            for cand in by_text.get(old_line["text"], []):
                if id(cand) in taken:
                    holder = owner.get(id(cand))
                    if holder is None or _exact(holder):
                        continue  # 완전히 맞는 짝은 못 뺏는다
                    pairs[holder] = (pairs[holder][0], None)
                if partner is not None:
                    taken.discard(id(partner))
                    owner.pop(id(partner), None)
                taken.add(id(cand))
                owner[id(cand)] = idx
                pairs[idx] = (old_line, cand)
                moved = True
                break
        if not moved:
            break

    # 3단계 — 판정. 짝이 없으면 삭제, 내용이 다르면 어절 단위 강조.
    for old_line, partner in pairs:
        if partner is None:
            old_line["status"] = "removed"
        elif partner["text"] != old_line["text"]:
            old_line["status"] = "changed"
            partner["status"] = "changed"
            old_line["html"] = _diff_old_html(old_line["text"], partner["text"])
            partner["html"] = _diff_new_html(old_line["text"], partner["text"])

    for line in new_lines:
        if id(line) not in taken:
            line["status"] = "added"


def register_law_routes(
    app: Flask, *,
    watchlist_repo: WatchlistRepo | None = None,
    version_repo: VersionRepo | None = None,
    client_factory: Callable[[], LawApiClient] | None = None,
    revision_lookup: Callable[[list[str]], dict[str, dict]] | None = None,
) -> None:
    """라우트 2개를 등록함. 세 의존성 모두 안 넘기면(기본값 None) 실
    Database/LawApiClient는 처음 /laws 요청이 올 때야 만듦(지연 생성)
    — create_app()을 부르기만 해도 곧바로 실 DB 커넥션이 열리는 걸
    막음(이 페이지와 무관한 대부분의 웹앱 테스트가 매번 그 비용을
    치르지 않도록).
    """
    _lazy: dict[str, object] = {}

    def _recent_revision(law_id: str) -> dict | None:
        """현재 열 머리에 붙일 "최근 개정" 배지 데이터 — /pdf 결과의 배지와
        같은 소스(change_log)·같은 90일 규칙을 씀(배지를 눌러 넘어온
        사용자가 같은 정보를 다시 보게). 조회 실패 시 None(배지 없음)."""
        from datetime import date, timedelta

        from webapp.pdfcheck import RECENT_REVISION_DAYS, fetch_revision_info

        lookup = revision_lookup or fetch_revision_info
        try:
            rev = lookup([law_id]).get(law_id) or {}
        except Exception:
            return None
        enforce = rev.get("enforce_date")
        if enforce is None or enforce < date.today() - timedelta(days=RECENT_REVISION_DAYS):
            return None
        return {"revision_type": rev.get("revision_type") or "개정", "enforce_date": enforce}

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
        except LawApiError as exc:
            # 신구법 비교 조회 자체가 실패한 경우(체인을 못 세움). 개별
            # 버전의 전문 실패는 build_version_chain 안에서 걸러지므로
            # 여기까지 오는 건 "이 법은 지금 아예 못 본다"는 상황임.
            log.warning("전문 비교를 만들지 못했습니다 (%s): %s", entry.official_name, exc)
            abort(503, f"{entry.official_name}: 지금 국가법령정보 API에서 전문을 받지 못했어요. 잠시 후 다시 시도해 주세요.")
        finally:
            client.close()

        if not chain:
            abort(503, f"{entry.official_name}: 이 법의 전문을 받지 못했어요. 잠시 후 다시 시도해 주세요.")

        n = len(chain)
        columns = [
            {
                "serial_no": v.serial_no,
                # 요구사항: "2100000272436" 같은 일련번호 대신
                # 시행일/공포일을 보여 줌 — 시행일이 없으면
                # 공포일로, 그것도 없으면(드묾) 일련번호로 최종 폴백함.
                #
                # 무슨 날짜인지 앞에 붙이는 이유: 라벨 없이 날짜만 두면
                # 시행일인지 공포일인지 알 수 없음. 시행이 유예된 개정이
                # 걸려 있으면 왼쪽(개정 전) 열의 날짜가 오른쪽보다 늦게
                # 보이는 일이 실제로 생기는데(개인정보 보호법 시행령:
                # 2월 공포·8월 시행분과 5월 공포·즉시 시행분이 함께 있음),
                # 그때 라벨이 없으면 순서가 뒤집힌 것처럼 읽힘.
                **_date_of(v),
                "is_current": v.serial_no == entry.last_serial_no,
                "label": _LABELS_BY_DISTANCE.get((n - 1) - i, f"{(n - 1) - i}단계 전"),
                "articles": _group_by_article(searchable_units_for(kind, v.full_text)),
                "diff_role": "",
            }
            for i, v in enumerate(chain)
        ]
        # 가장 최근 전환(직전 버전 -> 현재 버전)만 강조함 — depth=3이라
        # 전전 버전까지 있어도, 맨 왼쪽(전전) 열은 그냥 참고용 원문으로
        # 둠(가운데 열이 "전전과 비교한 강조"와 "후와 비교한 강조"를
        # 동시에 띠면 오히려 헷갈림).
        if len(columns) >= 2:
            _apply_diff_highlight(columns[-2], columns[-1])
            # report.html의 mark.diff-changed 색 규칙(fulltext-col--old=
            # 빨강, fulltext-col--new=파랑)이 그대로 먹도록, 강조 대상인
            # 마지막 두 열에만 같은 클래스를 붙임 — 새 색 규칙을 또
            # 만들지 않고 기존 개정 요약 페이지와 완전히 같은 색을 씀.
            columns[-2]["diff_role"] = "old"
            columns[-1]["diff_role"] = "new"
        # depth=3을 요청했는데 체인이 그만큼 안 나왔다 = 최초 제정본까지
        # 거슬러 올라가 더 이전 버전이 없다는 뜻(오류 아님) — 화면에
        # 그 사실을 알려줌. depth=2일 땐 "전전 버전 보기" 버튼을 그냥
        # 항상 보여줌(눌러봐야 있는지 없는지 아니까).
        no_earlier_version = depth == _MAX_DEPTH and len(chain) < depth
        show_expand_button = depth < _MAX_DEPTH

        return render_template(
            "law_detail.html", entry=entry, columns=columns, depth=depth,
            show_expand_button=show_expand_button, no_earlier_version=no_earlier_version,
            recent_revision=_recent_revision(law_id),
        )
