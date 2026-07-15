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

    change_rows = _change_rows_in_period(by_law_serial)

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

    contract = WeeklyContract(
        batch_date=batch_date.isoformat(),
        period=Period(from_date=from_date.isoformat(), to_date=to_date.isoformat()),
        amendment_groups=amendment_groups,
        unresolved=unresolved,
        no_comparison=no_comparison,
    )
    log.info("계약 조립 완료: %s", contract.summary())
    return contract


def _change_rows_in_period(by_law_serial: dict[tuple[str, str], list[dict]]) -> list[dict]:
    """article_diff 에서 확보한 (law_id, serial_no) 조합별 메타 정보 집계.

    ❓ 현재는 promulgation_no 를 article_diff 행에서 직접 끌어오지 않는다
    (article_diff 스키마에 그 컬럼이 없음 — change_log 에만 있음).
    운영 단계에서는 ChangeLogRepo 에 기간 조회 메서드(fetch_period)를
    추가해 change_log 를 조인하는 편이 더 정확하다. 지금은 article_diff
    가 이미 갖고 있는 (law_id, serial_no) 쌍을 뼈대로 최소 구현했다.
    """
    rows = []
    for (law_id, serial_no), diffs in by_law_serial.items():
        rows.append({
            "law_id": law_id,
            "new_serial_no": serial_no,
            "promulgation_no": diffs[0].get("promulgation_no", "") if diffs else "",
            "promulgation_date": "",
            "revision_type": "",
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
                new_serial_no=serial_no,
                enforce_date=str(diffs[0].get("enforce_date") or ""),
                source_url=url,
                articles=articles,
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