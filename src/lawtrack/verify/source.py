"""WeeklyContract가 이번 배치의 감지 결과와 일치하는지 코드로 검증한다."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lawtrack.contract.schema import (
    VerificationIssue,
    VerificationReport,
    WeeklyContract,
)

_ALLOWED_LAW_TYPES = {"법률", "시행령", "시행규칙", "행정규칙"}
_ALLOWED_CHANGE_TYPES = {"개정", "신설", "삭제", "미상"}
_ALLOWED_MATCH_STATUSES = {
    "성공",
    "삭제(위치탐색제외)",
    "위치재배치의심",
}
_SECRET_QUERY_RE = re.compile(r"(?:^|[?&])(?:OC|api[_-]?key)=", re.IGNORECASE)


def source_sha256(contract: WeeklyContract) -> str:
    """LLM 산출물과 검증 메타데이터를 제외한 원본 계약의 안정적 해시."""
    payload = contract.model_dump(
        mode="json",
        exclude={"llm_summary", "verification"},
    )
    return _sha256(payload)


def summary_sha256(contract: WeeklyContract) -> str:
    """검증 대상 LLM 요약의 안정적 해시. 요약이 없으면 빈 문자열."""
    if contract.llm_summary is None:
        return ""
    return _sha256(contract.llm_summary.model_dump(mode="json"))


def verify_source_integrity(
    contract: WeeklyContract,
    *,
    expected_versions: set[tuple[str, str]],
    processing_errors: list[tuple[str, str, str]] | None = None,
    secrets: tuple[str, ...] = (),
    verified_at: datetime | None = None,
) -> VerificationReport:
    """감지기가 넘긴 버전 집합과 계약의 법령·조문 구조를 교차 검증한다.

    같은 국가법령정보 API를 다시 호출하는 방식은 독립 검증이 아니고 배치
    실패 지점만 늘리므로, 감지 단계가 확정한 ``(law_id, serial_no)`` 집합과
    DB에서 조립된 계약을 대조한다. ID·일련번호 누락/혼입, 비밀값 노출,
    깨진 조문 구조는 LLM 호출 전에 차단한다.
    """
    issues: list[VerificationIssue] = []
    expected = set(expected_versions)
    for law_id, law_name, detail in processing_errors or ():
        issues.append(_source_error(
            "BATCH_ITEM_FAILED",
            "감시 대상 처리에 실패해 이번 배치가 완전하지 않습니다: "
            + _redact(detail, secrets),
            law_id=law_id,
            evidence=law_name,
        ))
    law_versions: list[tuple[str, str]] = [
        (law.law_id, law.new_serial_no)
        for group in contract.amendment_groups
        for law in group.laws
    ]
    no_comparison_versions = [
        (item.law_id, item.new_serial_no)
        for item in contract.no_comparison
    ]
    all_versions = law_versions + no_comparison_versions
    actual = set(all_versions)

    for law_id, serial_no in sorted(expected - actual):
        issues.append(_source_error(
            "EXPECTED_VERSION_MISSING",
            "감지된 최신 버전이 계약 JSON에서 누락되었습니다.",
            law_id=law_id,
            serial_no=serial_no,
        ))
    for law_id, serial_no in sorted(actual - expected):
        issues.append(_source_error(
            "UNEXPECTED_VERSION",
            "이번 배치에서 감지하지 않은 법령 버전이 계약 JSON에 포함되었습니다.",
            law_id=law_id,
            serial_no=serial_no,
        ))

    seen_versions: set[tuple[str, str]] = set()
    for law_id, serial_no in all_versions:
        version = (law_id, serial_no)
        if version in seen_versions:
            issues.append(_source_error(
                "DUPLICATE_VERSION",
                "같은 법령 버전이 계약에 중복 포함되었습니다.",
                law_id=law_id,
                serial_no=serial_no,
            ))
        seen_versions.add(version)

    seen_group_ids: set[str] = set()
    unresolved_versions = {
        (item.law_id, item.new_serial_no)
        for item in contract.unresolved
    }
    for group in contract.amendment_groups:
        if not group.group_id.strip():
            issues.append(_source_error(
                "EMPTY_GROUP_ID",
                "개정 그룹 식별자가 비어 있습니다.",
            ))
        elif group.group_id in seen_group_ids:
            issues.append(_source_error(
                "DUPLICATE_GROUP_ID",
                "개정 그룹 식별자가 중복되었습니다.",
                evidence=group.group_id,
            ))
        seen_group_ids.add(group.group_id)

        group_law_ids = [law.law_id for law in group.laws]
        if set(group.affected_law_ids) != set(group_law_ids):
            issues.append(_source_error(
                "GROUP_LAW_IDS_MISMATCH",
                "affected_law_ids와 그룹 안의 실제 법령 ID가 일치하지 않습니다.",
                evidence=group.group_id,
            ))

        for law in group.laws:
            _check_law(
                law,
                unresolved_versions=unresolved_versions,
                secrets=secrets,
                issues=issues,
            )

    for item in contract.no_comparison:
        if not item.law_name.strip():
            issues.append(_source_error(
                "EMPTY_LAW_NAME",
                "비교불가 법령의 법령명이 비어 있습니다.",
                law_id=item.law_id,
                serial_no=item.new_serial_no,
            ))
        _check_source_url(
            item.source_url,
            item.new_serial_no,
            law_id=item.law_id,
            secrets=secrets,
            issues=issues,
        )

    for item in contract.unresolved:
        if (item.law_id, item.new_serial_no) not in set(law_versions):
            issues.append(_source_error(
                "ORPHAN_UNRESOLVED_ITEM",
                "미확정 항목이 계약의 법령 버전과 연결되지 않습니다.",
                law_id=item.law_id,
                serial_no=item.new_serial_no,
            ))
        _check_source_url(
            item.source_url,
            item.new_serial_no,
            law_id=item.law_id,
            secrets=secrets,
            issues=issues,
        )

    _check_iso_date(contract.batch_date, "batch_date", issues)
    _check_iso_date(contract.period.from_date, "period.from_date", issues)
    _check_iso_date(contract.period.to_date, "period.to_date", issues)

    status = "FAIL" if any(issue.severity == "ERROR" for issue in issues) else (
        "WARN" if issues else "PASS"
    )
    return VerificationReport(
        status=status,
        source_integrity=status,
        summary_grounding="NOT_RUN",
        verified_at=(verified_at or datetime.now()).isoformat(timespec="seconds"),
        source_sha256=source_sha256(contract),
        expected_version_count=len(expected),
        contract_version_count=len(actual),
        checked_law_count=len(law_versions),
        issues=issues,
    )


def write_verification_report(
    report: VerificationReport,
    output_dir: Path,
    *,
    batch_date: str,
) -> Path:
    """HWPX와 독립된 검증 보고서 JSON을 저장한다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"verification_report_{batch_date}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def _check_law(
    law,
    *,
    unresolved_versions: set[tuple[str, str]],
    secrets: tuple[str, ...],
    issues: list[VerificationIssue],
) -> None:
    version = (law.law_id, law.new_serial_no)
    if not law.law_name.strip():
        issues.append(_source_error(
            "EMPTY_LAW_NAME",
            "법령명이 비어 있습니다.",
            law_id=law.law_id,
            serial_no=law.new_serial_no,
        ))
    if law.law_type not in _ALLOWED_LAW_TYPES:
        issues.append(_source_error(
            "INVALID_LAW_TYPE",
            "지원하지 않는 법령 종류입니다.",
            law_id=law.law_id,
            serial_no=law.new_serial_no,
            evidence=law.law_type,
        ))
    if not law.new_serial_no.strip():
        issues.append(_source_error(
            "EMPTY_SERIAL_NO",
            "최신 법령 일련번호가 비어 있습니다.",
            law_id=law.law_id,
        ))
    if (
        not law.articles
        and not law.structural_expansions
        and version not in unresolved_versions
    ):
        issues.append(_source_error(
            "EMPTY_CHANGE_DETAILS",
            "변경 법령에 조문·구조확장·미확정 세부내역이 모두 없습니다.",
            law_id=law.law_id,
            serial_no=law.new_serial_no,
        ))

    _check_source_url(
        law.source_url,
        law.new_serial_no,
        law_id=law.law_id,
        secrets=secrets,
        issues=issues,
    )

    seen_rows: set[tuple[str, str, str, str]] = set()
    for article in law.articles:
        location = article.location_label
        if not article.article_label.strip():
            issues.append(_source_error(
                "EMPTY_ARTICLE_LABEL",
                "조문 변경의 조문 라벨이 비어 있습니다.",
                law_id=law.law_id,
                serial_no=law.new_serial_no,
            ))
        if article.change_type not in _ALLOWED_CHANGE_TYPES:
            issues.append(_source_error(
                "INVALID_CHANGE_TYPE",
                "알 수 없는 조문 변경 유형입니다.",
                law_id=law.law_id,
                serial_no=law.new_serial_no,
                location=location,
                evidence=article.change_type,
            ))
        if article.match_status not in _ALLOWED_MATCH_STATUSES:
            issues.append(_source_error(
                "INVALID_MATCH_STATUS",
                "계약의 articles에 허용되지 않는 위치확정 상태가 들어 있습니다.",
                law_id=law.law_id,
                serial_no=law.new_serial_no,
                location=location,
                evidence=article.match_status,
            ))
        if not article.old_text.strip() and not article.new_text.strip():
            issues.append(_source_error(
                "EMPTY_DIFF_TEXT",
                "개정 전·후 원문이 모두 비어 있습니다.",
                law_id=law.law_id,
                serial_no=law.new_serial_no,
                location=location,
            ))
        fingerprint = (
            location,
            article.change_type,
            article.old_text,
            article.new_text,
        )
        if fingerprint in seen_rows:
            issues.append(_source_error(
                "DUPLICATE_ARTICLE_DIFF",
                "동일한 조문 변경 행이 중복되었습니다.",
                law_id=law.law_id,
                serial_no=law.new_serial_no,
                location=location,
            ))
        seen_rows.add(fingerprint)

    for expansion in law.structural_expansions:
        if not expansion.article_label.strip() or not expansion.new_items:
            issues.append(_source_error(
                "INVALID_STRUCTURAL_EXPANSION",
                "구조확장 그룹에 조문 라벨 또는 신법 항목이 없습니다.",
                law_id=law.law_id,
                serial_no=law.new_serial_no,
                location=expansion.article_label,
            ))
        for item in expansion.new_items:
            if not item.text.strip():
                issues.append(_source_error(
                    "EMPTY_EXPANSION_TEXT",
                    "구조확장의 신법 원문이 비어 있습니다.",
                    law_id=law.law_id,
                    serial_no=law.new_serial_no,
                    location=(
                        expansion.article_label
                        + item.clause_no
                        + item.item_label
                        + item.subitem_label
                    ),
                ))


def _check_source_url(
    url: str,
    serial_no: str,
    *,
    law_id: str,
    secrets: tuple[str, ...],
    issues: list[VerificationIssue],
) -> None:
    if not url:
        issues.append(_source_error(
            "EMPTY_SOURCE_URL",
            "법제처 원문 URL이 비어 있습니다.",
            law_id=law_id,
            serial_no=serial_no,
        ))
        return
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"law.go.kr", "www.law.go.kr"}:
        issues.append(_source_error(
            "INVALID_SOURCE_URL",
            "법제처 HTTPS 원문 URL 형식이 아닙니다.",
            law_id=law_id,
            serial_no=serial_no,
            evidence=url,
        ))
    if _SECRET_QUERY_RE.search(url) or any(
        secret and secret in url for secret in secrets
    ):
        issues.append(_source_error(
            "SECRET_IN_SOURCE_URL",
            "원문 URL에 API 인증정보가 포함되어 있습니다.",
            law_id=law_id,
            serial_no=serial_no,
        ))
    query = parse_qs(parsed.query)
    target = (query.get("target") or [""])[0]
    serial_key = "ID" if target == "admrul" else "MST"
    linked_serial = (query.get(serial_key) or [""])[0]
    if linked_serial != serial_no:
        issues.append(_source_error(
            "SOURCE_URL_SERIAL_MISMATCH",
            "원문 URL의 일련번호와 계약의 최신 일련번호가 다릅니다.",
            law_id=law_id,
            serial_no=serial_no,
            evidence=linked_serial,
        ))


def _check_iso_date(
    value: str,
    field: str,
    issues: list[VerificationIssue],
) -> None:
    try:
        date.fromisoformat(value)
    except ValueError:
        issues.append(_source_error(
            "INVALID_DATE",
            "YYYY-MM-DD 형식의 날짜가 아닙니다.",
            field=field,
            evidence=value,
        ))


def _source_error(
    code: str,
    reason: str,
    *,
    law_id: str = "",
    serial_no: str = "",
    location: str = "",
    field: str = "",
    evidence: str = "",
) -> VerificationIssue:
    return VerificationIssue(
        severity="ERROR",
        category="SOURCE",
        code=code,
        law_id=law_id,
        new_serial_no=serial_no,
        location=location,
        field=field,
        evidence=evidence,
        reason=reason,
    )


def _sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    redacted = str(value)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted
