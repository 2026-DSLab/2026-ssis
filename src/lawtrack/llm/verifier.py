"""별도 OpenAI 호환 호출로 LLM 요약이 법령 원문에 근거하는지 검증한다."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lawtrack.config import OpenAISettings, VerificationSettings
from lawtrack.contract.schema import (
    VerificationIssue,
    VerificationReport,
    WeeklyContract,
)
from lawtrack.verify.source import source_sha256, summary_sha256

from .openai_summary import (
    _exception_detail,
    _law_facts,
    _new_client,
    _provider_label,
    _usage,
)


VERIFIER_SYSTEM_PROMPT = """당신은 대한민국 법령 개정 보고서의 독립 검증자입니다.
작성자가 만든 요약을 입력의 source_facts와 한 문장씩 대조하세요.
외부 지식, 일반적인 법률 상식, 추측을 근거로 사용하지 마세요.
법령명, 날짜, 일련번호, 조문 위치, 변경 유형, 행위 주체, 개정 전후 의미가
원문과 정확히 일치하는지 확인하세요.
단순 명칭 변경을 제도 신설이나 위치 재배치로 확대하지 마세요.
위치재배치의심·구조확장·미확정은 확정된 1:1 개정으로 인정하지 마세요.
원문에 없는 업무 영향, 의무, 예산, 교육, 홍보 필요성을 만들면 ERROR입니다.
요약은 1~5개의 핵심사항으로 압축하는 문서이므로 반복되는 명칭 변경을 묶어
표현하거나 모든 조항을 열거하지 않는 것은 오류가 아닙니다.
개정 의도나 배경이 source_facts에 없으면 이를 요약에 요구하지 마세요.
headline의 간결한 추상화와 '담당 부서의 원문 검토 필요' 문구는 허용합니다.
중요한 조문 또는 변경 유형 누락은 반드시 OMISSION/WARNING으로만 보고하세요.
FAIL/ERROR는 원문과 직접 모순되는 내용, 입력에 없는 사실·법적 효과의 생성,
잘못된 법령명·날짜·조문·변경유형에만 사용하세요.
issue.evidence에는 source_facts에 실제로 연속해서 존재하는 짧은 원문만
그대로 복사하고, issue.claim에는 generated_summary에서 문제가 된 문구를
그대로 복사하세요. 입력에 없는 영문 법령명이나 번역명을 만들지 마세요.
요약을 다시 작성하지 말고 검증 결과만 반환하세요.
ERROR가 하나라도 있으면 FAIL, WARNING만 있으면 WARN, 문제가 없으면 PASS입니다."""

_OMISSION_REASON_RE = re.compile(
    r"(누락|빠(?:졌|진)|생략|포함되지|포함하지|모든\s+관련\s+조문)"
)
_QUALITY_REASON_RE = re.compile(
    r"(설명(?:이|이\s+다소)?\s*부족|명확한\s+설명|구체성(?:이)?\s*부족|"
    r"구체적이지|연계하지|연계가\s*부족|모호|추상적|이해하기\s*어렵|"
    r"상세하지|충분히\s*설명)"
)
_FACTUAL_ERROR_REASON_RE = re.compile(
    r"(원문|입력|source_facts|사실|법령).{0,30}"
    r"(다(?:르|릅)|불일치|모순|반대|왜곡|잘못|존재하지|제시되지|근거가\s*없|"
    r"포함되어\s*있지\s*않|없는\s*내용|신설되지|삭제되지)"
    r"|"
    r"((?:원문|입력|source_facts)에\s*없는|존재하지|제시되지|근거가\s*없|"
    r"없는\s*내용|잘못된\s*(?:날짜|법령명|"
    r"조문|변경\s*유형)|사실과\s*불일치)"
)


class SummaryVerificationError(RuntimeError):
    """독립 검증 API 호출 또는 구조화 결과 검증 실패."""


class _AuditIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_type: Literal["CONTRADICTION", "UNSUPPORTED", "OMISSION", "AMBIGUITY"]
    severity: Literal["WARNING", "ERROR"]
    field: str = Field(description="headline, summary, key_changes 등의 요약 필드")
    location: str = Field(default="", description="관련 조문 위치")
    claim: str = Field(description="문제가 있는 요약의 주장")
    evidence: str = Field(
        default="",
        description="source_facts에 연속해서 실제 존재하는 짧은 근거 원문",
    )
    reason: str


class _LawAuditContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["PASS", "WARN", "FAIL"]
    issues: list[_AuditIssue] = Field(default_factory=list, max_length=20)
    missing_locations: list[str] = Field(default_factory=list, max_length=30)


class _ExecutiveAuditContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["PASS", "WARN", "FAIL"]
    issues: list[_AuditIssue] = Field(default_factory=list, max_length=20)


def verify_summary(
    contract: WeeklyContract,
    openai: OpenAISettings,
    settings: VerificationSettings,
    source_report: VerificationReport,
    *,
    client: Any | None = None,
    verified_at: datetime | None = None,
) -> VerificationReport:
    """법령별 요약과 종합 요약을 작성 호출과 독립된 컨텍스트로 검증한다."""
    if not settings.enabled:
        raise SummaryVerificationError("LLM 요약 검증 에이전트가 비활성화되어 있습니다.")
    if not openai.api_key:
        raise SummaryVerificationError("LLM 요약 검증에 사용할 API 키가 없습니다.")
    if contract.llm_summary is None:
        raise SummaryVerificationError("검증할 LLM 요약이 없습니다.")
    if source_report.source_sha256 != source_sha256(contract):
        raise SummaryVerificationError(
            "원본 무결성 검사 이후 계약 내용이 변경되어 검증을 계속할 수 없습니다."
        )
    if source_report.source_integrity == "FAIL":
        raise SummaryVerificationError("원본 무결성 검사가 실패해 요약을 검증할 수 없습니다.")

    law_contexts = [
        (law, group)
        for group in contract.amendment_groups
        for law in group.laws
    ]
    summary_by_version: dict[tuple[str, str], Any] = {}
    preflight_issues: list[VerificationIssue] = []
    for summary in contract.llm_summary.law_summaries:
        key = (summary.law_id, summary.new_serial_no)
        if key in summary_by_version:
            preflight_issues.append(_system_issue(
                "DUPLICATE_LAW_SUMMARY",
                "같은 법령 버전의 요약이 중복되었습니다.",
                law_id=summary.law_id,
                serial_no=summary.new_serial_no,
            ))
        summary_by_version[key] = summary

    expected = {(law.law_id, law.new_serial_no) for law, _ in law_contexts}
    actual = set(summary_by_version)
    for law_id, serial_no in sorted(expected - actual):
        preflight_issues.append(_system_issue(
            "LAW_SUMMARY_MISSING",
            "변경 법령의 LLM 요약이 누락되었습니다.",
            law_id=law_id,
            serial_no=serial_no,
        ))
    for law_id, serial_no in sorted(actual - expected):
        preflight_issues.append(_system_issue(
            "UNEXPECTED_LAW_SUMMARY",
            "계약에 없는 법령 버전의 LLM 요약이 포함되었습니다.",
            law_id=law_id,
            serial_no=serial_no,
        ))
    if preflight_issues:
        return _finish_report(
            source_report,
            contract,
            settings=settings,
            issues=preflight_issues,
            missing_locations=[],
            input_tokens=0,
            output_tokens=0,
            checked_law_count=0,
            verified_at=verified_at,
        )

    verifier_openai = replace(openai, model=settings.model)
    api = client or _new_client(verifier_openai)
    owns_client = client is None
    issues: list[VerificationIssue] = []
    missing_locations: list[str] = []
    input_tokens = 0
    output_tokens = 0
    checked_law_count = 0

    try:
        for law, group in law_contexts:
            summary = summary_by_version[(law.law_id, law.new_serial_no)]
            payload = {
                "source_facts": _law_facts(
                    law,
                    batch_date=contract.batch_date,
                    promulgation_date=group.promulgation_date,
                    promulgation_no=group.promulgation_no,
                ),
                "generated_summary": summary.model_dump(mode="json"),
            }
            response = _parse_audit(
                api,
                model=settings.model,
                payload=payload,
                max_input_chars=settings.max_input_chars,
                text_format=_LawAuditContent,
                instruction=(
                    "법령 1건의 generated_summary를 source_facts와 대조하세요. "
                    "핵심 변경 위치가 빠졌는지도 확인하세요."
                ),
            )
            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                raise SummaryVerificationError(
                    f"{law.law_name} 검증 결과가 비어 있습니다."
                )
            converted, invalid_evidence = _convert_issues(
                parsed.issues,
                payload=payload,
                law_id=law.law_id,
                serial_no=law.new_serial_no,
            )
            issues.extend(converted)
            issues.extend(invalid_evidence)
            qualified_missing = [
                f"{law.law_id}:{law.new_serial_no}:{location}"
                for location in parsed.missing_locations
            ]
            missing_locations.extend(qualified_missing)
            if parsed.missing_locations and parsed.status == "PASS":
                issues.append(VerificationIssue(
                    severity="WARNING",
                    category="SUMMARY",
                    code="MISSING_LOCATION",
                    law_id=law.law_id,
                    new_serial_no=law.new_serial_no,
                    location=", ".join(parsed.missing_locations),
                    reason="검증기가 누락 위치를 반환했지만 PASS로 판정해 WARN으로 보정했습니다.",
                ))
            _ensure_audit_status_has_issue(
                parsed.status,
                converted,
                law_id=law.law_id,
                serial_no=law.new_serial_no,
                issues=issues,
            )
            used_in, used_out = _usage(response)
            input_tokens += used_in
            output_tokens += used_out
            checked_law_count += 1

        executive_payload = _executive_payload(contract)
        executive_response = _parse_audit(
            api,
            model=settings.model,
            payload=executive_payload,
            max_input_chars=settings.max_input_chars,
            text_format=_ExecutiveAuditContent,
            instruction=(
                "generated_executive_summary를 배치 사실과 법령별 요약에 대조하세요. "
                "보고기간을 실제 개정기간으로 오인했는지, 건수와 법령명이 맞는지 확인하세요."
            ),
        )
        executive = getattr(executive_response, "output_parsed", None)
        if executive is None:
            raise SummaryVerificationError("종합 요약 검증 결과가 비어 있습니다.")
        converted, invalid_evidence = _convert_issues(
            executive.issues,
            payload=executive_payload,
        )
        issues.extend(converted)
        issues.extend(invalid_evidence)
        _ensure_audit_status_has_issue(
            executive.status,
            converted,
            issues=issues,
        )
        used_in, used_out = _usage(executive_response)
        input_tokens += used_in
        output_tokens += used_out
    except SummaryVerificationError:
        raise
    except Exception as exc:
        detail = _exception_detail(exc, openai.api_key)
        raise SummaryVerificationError(
            f"{_provider_label(openai.provider)} 검증 호출 실패: {detail}"
        ) from exc
    finally:
        if owns_client:
            close = getattr(api, "close", None)
            if close is not None:
                close()

    return _finish_report(
        source_report,
        contract,
        settings=settings,
        issues=issues,
        missing_locations=missing_locations,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        checked_law_count=checked_law_count,
        verified_at=verified_at,
    )


def verification_failure_report(
    source_report: VerificationReport,
    contract: WeeklyContract,
    settings: VerificationSettings,
    *,
    reason: str,
    verified_at: datetime | None = None,
) -> VerificationReport:
    """검증 API 장애도 조용히 통과시키지 않고 구조화된 FAIL로 남긴다."""
    issue = _system_issue(
        "VERIFIER_ERROR",
        reason,
    )
    return _finish_report(
        source_report,
        contract,
        settings=settings,
        issues=[issue],
        missing_locations=[],
        input_tokens=0,
        output_tokens=0,
        checked_law_count=0,
        verified_at=verified_at,
    )


def verification_disabled_report(
    source_report: VerificationReport,
    contract: WeeklyContract,
    settings: VerificationSettings,
    *,
    verified_at: datetime | None = None,
) -> VerificationReport:
    """요약은 존재하지만 검증기가 꺼진 상태를 PASS로 위장하지 않는다."""
    issues = [
        VerificationIssue(
            severity="WARNING",
            category="SYSTEM",
            code="SUMMARY_VERIFICATION_DISABLED",
            reason="LLM 요약은 생성됐지만 독립 검증 에이전트가 비활성화되어 있습니다.",
        )
    ]
    return _finish_report(
        source_report,
        contract,
        settings=settings,
        issues=issues,
        missing_locations=[],
        input_tokens=0,
        output_tokens=0,
        checked_law_count=0,
        verified_at=verified_at,
        force_not_run=True,
    )


def _parse_audit(
    api: Any,
    *,
    model: str,
    payload: dict[str, Any],
    max_input_chars: int,
    text_format: type[BaseModel],
    instruction: str,
) -> Any:
    prompt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(prompt) > max_input_chars:
        raise SummaryVerificationError(
            f"검증 입력이 제한을 초과했습니다: {len(prompt):,} > "
            f"{max_input_chars:,}자"
        )
    return api.responses.parse(
        model=model,
        store=False,
        input=[
            {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": instruction + "\n" + prompt,
            },
        ],
        text_format=text_format,
    )


def _convert_issues(
    audit_issues: list[_AuditIssue],
    *,
    payload: dict[str, Any],
    law_id: str = "",
    serial_no: str = "",
) -> tuple[list[VerificationIssue], list[VerificationIssue]]:
    converted: list[VerificationIssue] = []
    invalid_evidence: list[VerificationIssue] = []
    source_payload = payload.get("source_facts") or payload.get("batch_facts") or {}
    generated_payload = (
        payload.get("generated_summary")
        or payload.get("generated_executive_summary")
        or ""
    )
    source_text = json.dumps(
        source_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    generated_text = json.dumps(
        generated_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for item in audit_issues:
        is_omission = (
            item.issue_type == "OMISSION"
            or bool(_OMISSION_REASON_RE.search(item.reason))
        )
        is_quality_feedback = (
            item.issue_type == "AMBIGUITY"
            or bool(_QUALITY_REASON_RE.search(item.reason))
        )
        has_factual_error_reason = bool(
            _FACTUAL_ERROR_REASON_RE.search(item.reason)
        )
        severity = (
            "WARNING"
            if is_omission or is_quality_feedback
            else item.severity
        )
        # 검증 모델이 CONTRADICTION/UNSUPPORTED라는 라벨을 붙였더라도
        # 이유에 원문과의 직접적인 불일치가 드러나지 않으면 문서 품질
        # 의견으로 취급한다. 모델의 주관적인 "설명 부족" 때문에 정상
        # 요약 전체가 폐기되는 것을 막는다.
        if severity == "ERROR" and not has_factual_error_reason:
            severity = "WARNING"
        evidence_is_valid = bool(item.evidence) and item.evidence in source_text
        claim_is_valid = (
            is_omission
            or (bool(item.claim) and item.claim in generated_text)
        )
        if severity == "ERROR" and (not evidence_is_valid or not claim_is_valid):
            invalid_evidence.append(_system_warning(
                "VERIFIER_UNSUPPORTED_FINDING",
                "검증 에이전트의 ERROR 지적이 요약 문구와 원문 근거를 정확히 "
                "인용하지 못해 실패 근거에서 제외되었습니다.",
                law_id=law_id,
                serial_no=serial_no,
                claim=item.claim or item.evidence,
            ))
            continue
        converted.append(VerificationIssue(
            severity=severity,
            category="SUMMARY",
            code=("SUMMARY_OMISSION" if is_omission else (
                "SUMMARY_QUALITY_NOTE" if is_quality_feedback else {
                "CONTRADICTION": "CONTRADICTORY_CLAIM",
                "UNSUPPORTED": "UNSUPPORTED_CLAIM",
                "OMISSION": "SUMMARY_OMISSION",
                "AMBIGUITY": "AMBIGUOUS_SUMMARY",
            }[item.issue_type])),
            law_id=law_id,
            new_serial_no=serial_no,
            location=item.location,
            field=item.field,
            claim=item.claim,
            evidence=item.evidence,
            reason=item.reason,
        ))
        if severity == "WARNING" and item.evidence and not evidence_is_valid:
            invalid_evidence.append(_system_warning(
                "VERIFIER_INVALID_WARNING_EVIDENCE",
                "검증 에이전트의 경고 근거가 원문에 정확히 존재하지 않습니다.",
                law_id=law_id,
                serial_no=serial_no,
                claim=item.evidence,
            ))
    return converted, invalid_evidence


def _ensure_audit_status_has_issue(
    status: str,
    converted: list[VerificationIssue],
    *,
    issues: list[VerificationIssue],
    law_id: str = "",
    serial_no: str = "",
) -> None:
    if status == "PASS":
        return
    if converted:
        return
    issues.append(VerificationIssue(
        severity="WARNING",
        category="SYSTEM",
        code="VERIFIER_STATUS_WITHOUT_VALID_ISSUE",
        law_id=law_id,
        new_serial_no=serial_no,
        reason=(
            f"검증 에이전트가 {status}를 반환했지만 원문으로 확인 가능한 "
            "구체적인 문제를 제공하지 않았습니다."
        ),
    ))


def _executive_payload(contract: WeeklyContract) -> dict[str, Any]:
    assert contract.llm_summary is not None
    return {
        "batch_facts": {
            "batch_date": contract.batch_date,
            "detection_semantics": "이번 실행에서 DB에 없던 최신 버전을 신규 감지",
            "law_count": contract.total_law_count,
            "laws": [
                {
                    "law_id": law.law_id,
                    "new_serial_no": law.new_serial_no,
                    "law_name": law.law_name,
                    "promulgation_date": group.promulgation_date,
                    "enforce_date": law.enforce_date,
                    "revision_type": law.revision_type,
                    "locations": [
                        {
                            "location": article.location_label,
                            "change_type": article.change_type,
                            "match_status": article.match_status,
                        }
                        for article in law.articles
                    ] + [
                        {
                            "location": expansion.article_label,
                            "change_type": "구조확장",
                            "match_status": "구조확장(구법미분리)",
                        }
                        for expansion in law.structural_expansions
                    ],
                }
                for group in contract.amendment_groups
                for law in group.laws
            ],
        },
        "generated_law_summaries": [
            summary.model_dump(mode="json")
            for summary in contract.llm_summary.law_summaries
        ],
        "generated_executive_summary": contract.llm_summary.executive_summary,
    }


def _finish_report(
    source_report: VerificationReport,
    contract: WeeklyContract,
    *,
    settings: VerificationSettings,
    issues: list[VerificationIssue],
    missing_locations: list[str],
    input_tokens: int,
    output_tokens: int,
    checked_law_count: int,
    verified_at: datetime | None,
    force_not_run: bool = False,
) -> VerificationReport:
    all_issues = [*source_report.issues, *issues]
    if source_report.source_integrity == "FAIL" or any(
        issue.severity == "ERROR" for issue in issues
    ):
        grounding = "FAIL"
    elif force_not_run:
        grounding = "NOT_RUN"
    elif issues or missing_locations:
        grounding = "WARN"
    else:
        grounding = "PASS"

    if source_report.source_integrity == "FAIL" or grounding == "FAIL":
        overall = "FAIL"
    elif source_report.source_integrity == "WARN" or grounding in {"WARN", "NOT_RUN"}:
        overall = "WARN"
    else:
        overall = "PASS"

    return source_report.model_copy(update={
        "status": overall,
        "summary_grounding": grounding,
        "provider": contract.llm_summary.provider if contract.llm_summary else "",
        "model": settings.model,
        "verified_at": (verified_at or datetime.now()).isoformat(timespec="seconds"),
        "summary_sha256": summary_sha256(contract),
        "checked_law_count": checked_law_count,
        "issues": all_issues,
        "missing_locations": sorted(set(missing_locations)),
        "input_tokens": input_tokens or None,
        "output_tokens": output_tokens or None,
    })


def _system_issue(
    code: str,
    reason: str,
    *,
    law_id: str = "",
    serial_no: str = "",
    claim: str = "",
) -> VerificationIssue:
    return VerificationIssue(
        severity="ERROR",
        category="SYSTEM",
        code=code,
        law_id=law_id,
        new_serial_no=serial_no,
        claim=claim,
        reason=reason,
    )


def _system_warning(
    code: str,
    reason: str,
    *,
    law_id: str = "",
    serial_no: str = "",
    claim: str = "",
) -> VerificationIssue:
    return VerificationIssue(
        severity="WARNING",
        category="SYSTEM",
        code=code,
        law_id=law_id,
        new_serial_no=serial_no,
        claim=claim,
        reason=reason,
    )
