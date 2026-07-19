"""DB → WeeklyContract → JSON 파일.

이 파일이 사용자 역할의 종착점이다: "DB 가져와서 확인 후, 바뀐 부분을
구조화된 형태로 LLM팀에게 넘겨주는" 마지막 단계.

주의 — API 키 노출 방지:
    목록조회 응답의 '법령상세링크' 등에는 실제 호출에 쓰인 OC 인증키가
    쿼리스트링에 그대로 박혀 있다(실측: '/DRF/lawService.do?OC=joonone
    &target=law&MST=...'). 이걸 그대로 산출물에 옮기면 인증키가
    LLM팀 산출물(파일)에 새어나간다. source_url 생성 시 반드시 OC 를
    제거하거나 안전한 값으로 치환한다.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

from lawtrack.contract.schema import (
    AmendmentGroup,
    ArticleDiffItem,
    LawChange,
    NoComparisonItem,
    Period,
    UnresolvedItem,
    WeeklyContract,
)
from lawtrack.db.repo import ArticleDiffRepo, ChangeLogRepo, WatchlistRepo
from lawtrack.link import group_many

log = logging.getLogger(__name__)

_OC_PARAM_RE = re.compile(r"([?&])OC=[^&]*")

_SOURCE_URL_TEMPLATES = {
    "law": "https://www.law.go.kr/DRF/lawService.do?target=law&MST={serial}&type=HTML",
    "행정규칙": "https://www.law.go.kr/DRF/lawService.do?target=admrul&ID={serial}&type=HTML",
}


def _sanitize_url(url: str) -> str:
    """쿼리스트링의 OC 인증키를 제거."""
    if not url:
        return ""
    cleaned = _OC_PARAM_RE.sub(r"\1", url)
    return cleaned.replace("&&", "&").rstrip("&?")


def _source_url(law_type: str, serial_no: str) -> str:
    key = "행정규칙" if law_type == "행정규칙" else "law"
    return _SOURCE_URL_TEMPLATES[key].format(serial=serial_no)


def build_contract(
    watchlist_repo: WatchlistRepo,
    article_diff_repo: ArticleDiffRepo,
    change_log_repo: ChangeLogRepo,
    *,
    from_date: date,
    to_date: date,
    batch_date: date | None = None,
) -> WeeklyContract:
    """DB 에서 기간 내 변경분을 모아 WeeklyContract 를 조립한다."""
    batch_date = batch_date or date.today()

    diff_rows = article_diff_repo.fetch_period(from_date, to_date)
    by_law_serial: dict[tuple[str, str], list[dict]] = {}
    for row in diff_rows:
        key = (row["law_id"], row["law_serial_no"])
        by_law_serial.setdefault(key, []).append(row)

    change_rows = _change_rows_in_period(by_law_serial, change_log_repo)

    promulgation_nos = [r["promulgation_no"] for r in change_rows if r.get("promulgation_no")]
    linked = group_many(change_log_repo, promulgation_nos)

    groups_by_promulgation: dict[str, list[dict]] = {}
    standalone: list[dict] = []
    for row in change_rows:
        no = row.get("promulgation_no") or ""
        if no and linked.get(no) and linked[no].is_chained:
            groups_by_promulgation.setdefault(no, []).append(row)
        else:
            standalone.append(row)

    amendment_groups: list[AmendmentGroup] = []
    unresolved: list[UnresolvedItem] = []
    no_comparison: list[NoComparisonItem] = []

    for no, rows in groups_by_promulgation.items():
        laws, u, nc = _build_laws(rows, watchlist_repo, by_law_serial)
        unresolved.extend(u)
        no_comparison.extend(nc)
        amendment_groups.append(
            AmendmentGroup(
                group_id=no,
                promulgation_no=no,
                promulgation_date=rows[0].get("promulgation_date", "") or "",
                revision_type=rows[0].get("revision_type", "") or "",
                affected_law_ids=[r["law_id"] for r in rows],
                laws=laws,
            )
        )

    if standalone:
        laws, u, nc = _build_laws(standalone, watchlist_repo, by_law_serial)
        unresolved.extend(u)
        no_comparison.extend(nc)
        for law in laws:
            amendment_groups.append(
                AmendmentGroup(
                    group_id=f"single-{law.law_id}-{law.new_serial_no}",
                    promulgation_no="",
                    affected_law_ids=[law.law_id],
                    laws=[law],
                )
            )

    # ★★ 실측 발견(2026-07-18): 위 amendment_groups 조립 경로는 article_diff에서
    # 시작해 (law_id, serial_no) 집합을 얻는데, 신구법 대비가 아예 불가능한
    # 건(제정/폐지제정 등)은 정의상 article_diff 행이 0개라 그 집합에 원천적으로
    # 들어갈 수 없었다 — 즉 _build_laws의 no_comparison 처리 코드가 도달
    # 불가능한 죽은 코드였고, NoComparisonItem은 스키마만 있고 실제로 채워진
    # 적이 한 번도 없었다(제정된 법이 매번 산출물에서 통째로 사라짐). article_diff
    # 를 거치지 않고 change_log를 직접 조회해 채운다.
    seen = {(r["law_id"], r["new_serial_no"]) for r in change_rows}
    for pair in change_log_repo.fetch_no_comparison_in_period(from_date, to_date):
        law_id, serial_no = pair["law_id"], pair["new_serial_no"]
        if (law_id, serial_no) in seen:
            continue
        seen.add((law_id, serial_no))
        cl = change_log_repo.fetch_latest_for_serial(law_id, serial_no)
        entry = watchlist_repo.get(law_id)
        law_name = entry.official_name if entry else law_id
        law_type = entry.law_type if entry else ""
        url = _sanitize_url(_source_url(law_type, serial_no))
        no_comparison.append(
            NoComparisonItem(
                law_id=law_id, law_name=law_name, new_serial_no=serial_no,
                reason=(cl.get("revision_type") if cl else "") or "신구법 대비 불가",
                source_url=url,
            )
        )

    contract = WeeklyContract(
        batch_date=batch_date.isoformat(),
        period=Period(from_date=from_date.isoformat(), to_date=to_date.isoformat()),
        amendment_groups=amendment_groups,
        unresolved=unresolved,
        no_comparison=no_comparison,
    )
    log.info("계약 조립 완료: %s", contract.summary())
    return contract


def _change_rows_in_period(
    by_law_serial: dict[tuple[str, str], list[dict]], change_log_repo: ChangeLogRepo,
) -> list[dict]:
    """article_diff 에서 확보한 (law_id, serial_no) 조합별 메타 정보 집계.

    ✅ 실측 발견·수정(2026-07-16): promulgation_no 는 article_diff 스키마에
    아예 없는 컬럼이라(law_id, law_serial_no, article_code, … 뿐), 예전엔
    diffs[0].get("promulgation_no", "") 가 항상 기본값 ""로 떨어졌다.
    그 결과 연쇄개정 그룹핑(link.py group_many)의 입력이 늘 빈 문자열이라
    같은 공포번호로 동시개정된 법들도 전부 별도 그룹으로 쪼개져 나오는
    버그가 있었다(실측: 사회보장기본법/국민기초생활보장법이 같은
    공포번호 21065인데 그룹 3개로 쪼개짐). change_log(진짜 출처)에서
    (law_id, new_serial_no) 별 최신 1건을 조회해 채운다.
    """
    rows = []
    for law_id, serial_no in by_law_serial:
        cl = change_log_repo.fetch_latest_for_serial(law_id, serial_no)
        rows.append({
            "law_id": law_id,
            "new_serial_no": serial_no,
            "promulgation_no": (cl.get("promulgation_no") or "") if cl else "",
            "promulgation_date": "",
            "revision_type": (cl.get("revision_type") or "") if cl else "",
            "old_serial_no": (cl.get("old_serial_no") or "") if cl else "",
            "revision_reason": (cl.get("revision_reason") or "") if cl else "",
            "unchanged_clauses": (cl.get("unchanged_clauses") or {}) if cl else {},
        })
    return rows


def _build_laws(
    rows: list[dict], watchlist_repo: WatchlistRepo,
    by_law_serial: dict[tuple[str, str], list[dict]],
) -> tuple[list[LawChange], list[UnresolvedItem], list[NoComparisonItem]]:
    laws: list[LawChange] = []
    unresolved: list[UnresolvedItem] = []
    no_comparison: list[NoComparisonItem] = []

    for row in rows:
        law_id = row["law_id"]
        serial_no = row["new_serial_no"]
        entry = watchlist_repo.get(law_id)
        law_name = entry.official_name if entry else law_id
        law_type = entry.law_type if entry else ""
        internal_name = entry.internal_name if entry else ""
        dept_codes = list(entry.dept_codes) if entry else []

        diffs = by_law_serial.get((law_id, serial_no), [])
        url = _sanitize_url(_source_url(law_type, serial_no))

        if not diffs:
            # article_diff 행이 없다 = process_law_entry 의 NO_COMPARISON
            # 분기를 탄 경우 (신구법 대비 불가). 설계상 이 경우 article_diff
            # 에는 아무것도 안 쓰므로, 그 부재 자체가 신호가 된다.
            no_comparison.append(
                NoComparisonItem(
                    law_id=law_id, law_name=law_name, new_serial_no=serial_no,
                    reason="신구법 대비 불가", source_url=url,
                )
            )
            continue

        articles: list[ArticleDiffItem] = []
        for d in diffs:
            if d["match_status"] in ("0건실패", "중복실패"):
                detail = json.loads(d.get("match_detail") or "[]")
                unresolved.append(
                    UnresolvedItem(
                        law_id=law_id, law_name=law_name, new_serial_no=serial_no,
                        reason=d["match_status"], detail="; ".join(detail),
                        source_url=url, guards_tried=detail,
                    )
                )
                continue
            articles.append(
                ArticleDiffItem(
                    article_label=d.get("article_label") or d.get("article_code") or "",
                    clause_no=d.get("clause_no") or "",
                    item_label=d.get("item_label") or "",
                    subitem_label=d.get("subitem_label") or "",
                    change_type=d["change_type"],
                    old_text=d.get("old_text") or "",
                    new_text=d.get("new_text") or "",
                    match_status=d["match_status"],
                )
            )

        laws.append(
            LawChange(
                law_id=law_id, law_type=law_type, law_name=law_name,
                internal_name=internal_name, dept_codes=dept_codes,
                old_serial_no=row.get("old_serial_no", "") or "",
                new_serial_no=serial_no,
                enforce_date=str(diffs[0].get("enforce_date") or ""),
                revision_type=row.get("revision_type", "") or "",
                # ✅ 실측 발견·수정(2026-07-16): api/fulltext.py 가 "제개정이유"를
                # 이미 API 응답에서 뽑아오면서도(FullTextResult.revision_reason)
                # 그 값을 change_log 에 저장하지도, contract 에 담지도 않아
                # 항상 빈 문자열로 나갔다 — schema.py 의 설계 의도("LLM이
                # 추론할 필요 없게 함")를 무력화하고 있었다. change_log에
                # revision_reason 컬럼을 추가해 저장하고 여기서 채운다.
                revision_reason=row.get("revision_reason", "") or "",
                source_url=url,
                articles=articles,
                unchanged_clauses=row.get("unchanged_clauses") or {},
            )
        )

    return laws, unresolved, no_comparison


def write_contract(contract: WeeklyContract, output_dir: Path) -> Path:
    """검증된 계약을 JSON 파일로 저장. 파일명에 batch_date 를 포함해 덮어쓰기 방지."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"weekly_contract_{contract.batch_date}.json"
    path.write_text(
        contract.model_dump_json(indent=2, exclude_none=False),
        encoding="utf-8",
    )
    log.info("산출물 저장: %s (%s)", path, contract.summary())
    return path