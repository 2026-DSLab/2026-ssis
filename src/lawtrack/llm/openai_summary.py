"""OpenAI 호환 Responses API로 WeeklyContract의 확정 사실을 요약한다."""

from __future__ import annotations

import json
import re
import ssl
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lawtrack.config import OpenAISettings
from lawtrack.contract.schema import (
    LLMSummary,
    LawChange,
    LawLLMSummary,
    VerificationReport,
    WeeklyContract,
)


SYSTEM_PROMPT = """당신은 대한민국 법령 개정 주간보고서의 편집자입니다.
입력 JSON에 명시된 사실만 사용하고 외부 지식이나 법적 효과를 추측하지 마세요.
날짜, 법령명, 조문 위치, 개정 전후 문장을 새로 만들거나 바꾸지 마세요.
이 보고서의 기간은 실행 주기일 뿐입니다. '이번 주에 개정되었다'고 쓰지 말고
'이번 배치에서 신규 감지된 법령 버전'이라고 표현하세요.
입력 대상은 모두 이미 공포ㆍ발령된 법령 버전입니다. '개정안', '법안',
'발의', '입법예고'처럼 공포 전 단계로 표현하지 마세요.
batch_date와 enforce_date를 비교하여 이미 지난 시행일을 '시행 예정'이라고
쓰지 마세요. 날짜는 입력에 있는 연도와 날짜만 사용하세요.
key_changes의 각 문장은 반드시 입력에 실제로 존재하는 조문 번호로 시작하세요.
행위 주체와 위반행위를 정확히 구분하세요. 예를 들어 '인증을 받지 않고 표시함'을 '표시하지 않음'으로 바꾸면 안 됩니다.
revision_type이 '타법개정'이면 official_revision_reason은 다른 법률의 제정ㆍ개정 이유일 수 있습니다.
이 경우 현재 법령의 articles에 나타난 인용 법률ㆍ조문 정비만 설명하고, 다른 법률의 제도 신설을 현재 법령의 효과로 쓰지 마세요.
업무 영향이 입력만으로 확정되지 않으면 '담당 부서의 원문 검토 필요'라고 쓰세요.
위치재배치의심, 구조확장, 미확정 상태는 확정된 1:1 변경처럼 서술하지 마세요.
review_points에는 입력에서 직접 확인되는 대조 필요 사항만 쓰고, 일반적인 교육ㆍ홍보ㆍ예산ㆍ협력 필요성을 추측하지 마세요.
한국어로 작성하고, 입력 원문에 없는 한자나 중국어 표현을 새로 만들지 마세요.
간결하고 자연스러운 한국어 공공문서 문체를 사용하세요."""


class LLMSummaryError(RuntimeError):
    """LLM 요약 호출 또는 결과 검증 실패."""


class _LawSummaryContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(description="한 줄 핵심 제목")
    summary: str = Field(description="2~4문장의 개정 요약")
    key_changes: list[str] = Field(min_length=1, max_length=5, description="핵심 변경사항 1~5개")
    operational_impact: str = Field(description="확정 가능한 업무 영향 또는 검토 필요 문구")
    review_points: list[str] = Field(max_length=5, description="담당자가 확인할 사항 0~5개")


class _ExecutiveSummaryContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executive_summary: str = Field(description="이번 배치 신규 감지 결과를 3~5문장으로 정리한 요약")


def summarize_contract(
    contract: WeeklyContract,
    settings: OpenAISettings,
    *,
    client: Any | None = None,
    generated_at: datetime | None = None,
    verification_feedback: VerificationReport | None = None,
) -> WeeklyContract:
    """법령별 구조화 요약을 생성해 계약의 ``llm_summary``에 넣어 반환한다."""
    if not settings.configured:
        raise LLMSummaryError("LLM 요약이 활성화되지 않았거나 API 키가 없습니다.")

    law_contexts = [
        (law, group)
        for group in contract.amendment_groups
        for law in group.laws
    ]
    if not law_contexts:
        return contract.model_copy(update={"llm_summary": None})

    api = client or _new_client(settings)
    summaries: list[LawLLMSummary] = []
    input_tokens = 0
    output_tokens = 0
    previous_by_version = {
        (summary.law_id, summary.new_serial_no): summary
        for summary in (
            contract.llm_summary.law_summaries
            if contract.llm_summary is not None
            else []
        )
    }

    try:
        for law, group in law_contexts:
            facts = _law_facts(
                law,
                batch_date=contract.batch_date,
                promulgation_date=group.promulgation_date,
                promulgation_no=group.promulgation_no,
            )
            payload: dict[str, Any] = {"source_facts": facts}
            correction = _correction_context(
                verification_feedback,
                law_id=law.law_id,
                new_serial_no=law.new_serial_no,
                previous_summary=previous_by_version.get(
                    (law.law_id, law.new_serial_no)
                ),
            )
            if correction:
                payload["correction_context"] = correction
            validation_error = ""
            previous_candidate: dict[str, Any] | None = None
            parsed: _LawSummaryContent | None = None
            for attempt in range(2):
                attempt_payload = dict(payload)
                if validation_error:
                    attempt_payload["output_validation_feedback"] = {
                        "rejection_reason": validation_error,
                        "previous_candidate": previous_candidate,
                    }
                prompt = json.dumps(
                    attempt_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if len(prompt) > settings.max_input_chars:
                    raise LLMSummaryError(
                        f"{law.law_name}의 요약 입력이 제한을 초과했습니다: "
                        f"{len(prompt):,} > {settings.max_input_chars:,}자"
                    )
                response = api.responses.parse(
                    model=settings.model,
                    store=False,
                    input=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                "다음 source_facts의 법령 1건을 보고서용으로 요약하세요. "
                                "모든 핵심 변경 위치를 빠뜨리지 말고 중복 표현은 줄이세요.\n"
                                + (
                                    "correction_context가 있으면 이전 후보를 그대로 "
                                    "반복하지 말고, 원문으로 확인되는 검증 오류를 수정하세요.\n"
                                    if correction else ""
                                )
                                + (
                                    "output_validation_feedback가 있으면 거부 사유를 "
                                    "반영해 후보 전체를 다시 작성하세요.\n"
                                    if validation_error else ""
                                )
                                + prompt
                            ),
                        },
                    ],
                    text_format=_LawSummaryContent,
                )
                used_in, used_out = _usage(response)
                input_tokens += used_in
                output_tokens += used_out
                parsed = getattr(response, "output_parsed", None)
                if parsed is None:
                    validation_error = f"{law.law_name} 요약 결과가 비어 있습니다."
                    previous_candidate = None
                else:
                    try:
                        _validate_law_summary(law, parsed, facts)
                        break
                    except LLMSummaryError as exc:
                        validation_error = str(exc)
                        previous_candidate = parsed.model_dump(mode="json")
                if attempt == 1:
                    raise LLMSummaryError(validation_error)
            if parsed is None:
                raise LLMSummaryError(f"{law.law_name} 요약 결과가 비어 있습니다.")
            summaries.append(
                LawLLMSummary(
                    law_id=law.law_id,
                    new_serial_no=law.new_serial_no,
                    **parsed.model_dump(),
                )
            )

        executive_payload = {
            "batch_date": contract.batch_date,
            "detection_semantics": (
                "보고기간과 무관하게 이번 실행에서 DB에 없던 최신 버전을 신규 감지"
            ),
            "law_count": len(summaries),
            "laws": [
                {
                    "law_id": law.law_id,
                    "law_name": law.law_name,
                    "promulgation_date": group.promulgation_date,
                    "enforce_date": law.enforce_date,
                    "summary": summary.model_dump(),
                }
                for (law, group), summary in zip(law_contexts, summaries)
            ],
        }
        executive_correction = _correction_context(
            verification_feedback,
            previous_executive=(
                contract.llm_summary.executive_summary
                if contract.llm_summary is not None
                else ""
            ),
        )
        if executive_correction:
            executive_payload["correction_context"] = executive_correction

        executive_validation_error = ""
        previous_executive_candidate: dict[str, Any] | None = None
        executive: _ExecutiveSummaryContent | None = None
        for attempt in range(2):
            attempt_payload = dict(executive_payload)
            if executive_validation_error:
                attempt_payload["output_validation_feedback"] = {
                    "rejection_reason": executive_validation_error,
                    "previous_candidate": previous_executive_candidate,
                }
            executive_response = api.responses.parse(
                model=settings.model,
                store=False,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "다음 배치 사실만 사용해 경영진용 3~5문장으로 정리하세요. "
                            "첫 문장은 반드시 '이번 배치에서 신규 감지된 법령 버전은'으로 "
                            "시작하세요. 이번 주에 실제 개정되었다고 표현하지 말고, 개정 건수와 "
                            "검토 필요 사항을 명확히 쓰되 새로운 사실을 추가하지 마세요.\n"
                            + (
                                "correction_context가 있으면 원문으로 확인되는 기존 "
                                "종합 요약 오류를 수정하세요.\n"
                                if executive_correction else ""
                            )
                            + (
                                "output_validation_feedback가 있으면 거부 사유를 "
                                "반영해 후보 전체를 다시 작성하세요.\n"
                                if executive_validation_error else ""
                            )
                            + json.dumps(
                                attempt_payload,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        ),
                    },
                ],
                text_format=_ExecutiveSummaryContent,
            )
            used_in, used_out = _usage(executive_response)
            input_tokens += used_in
            output_tokens += used_out
            executive = getattr(executive_response, "output_parsed", None)
            if executive is None:
                executive_validation_error = "주간 종합 요약 결과가 비어 있습니다."
                previous_executive_candidate = None
            else:
                try:
                    _validate_executive_summary(contract, executive)
                    break
                except LLMSummaryError as exc:
                    executive_validation_error = str(exc)
                    previous_executive_candidate = executive.model_dump(mode="json")
            if attempt == 1:
                raise LLMSummaryError(executive_validation_error)
        if executive is None:
            raise LLMSummaryError("주간 종합 요약 결과가 비어 있습니다.")
    except LLMSummaryError:
        raise
    except Exception as exc:  # SDK별 예외를 안전한 도메인 오류로 통일
        detail = _exception_detail(exc, settings.api_key)
        provider = _provider_label(settings.provider)
        raise LLMSummaryError(f"{provider} 요약 호출 실패: {detail}") from exc

    llm_summary = LLMSummary(
        provider=settings.provider,
        model=settings.model,
        generated_at=(generated_at or datetime.now()).isoformat(timespec="seconds"),
        executive_summary=executive.executive_summary,
        law_summaries=summaries,
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
    )
    return contract.model_copy(update={"llm_summary": llm_summary})


def _correction_context(
    report: VerificationReport | None,
    *,
    law_id: str = "",
    new_serial_no: str = "",
    previous_summary: LawLLMSummary | None = None,
    previous_executive: str = "",
) -> dict[str, Any]:
    """검증된 ERROR만 1회 교정 프롬프트에 전달한다."""
    if report is None:
        return {}
    relevant = [
        issue.model_dump(mode="json")
        for issue in report.issues
        if (
            issue.severity == "ERROR"
            and issue.category == "SUMMARY"
            and (
                (law_id and issue.law_id == law_id and issue.new_serial_no == new_serial_no)
                or (not law_id and not issue.law_id)
            )
        )
    ]
    if not relevant:
        return {}
    context: dict[str, Any] = {"verified_errors": relevant}
    if previous_summary is not None:
        context["previous_summary"] = previous_summary.model_dump(mode="json")
    if previous_executive:
        context["previous_executive_summary"] = previous_executive
    return context


def _new_client(settings: OpenAISettings) -> Any:
    try:
        import httpx
        import truststore
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - requirements 설치 안내용
        raise LLMSummaryError(
            "openai 또는 truststore 패키지가 없습니다. "
            "pip install -r requirements.txt 를 실행하세요."
        ) from exc
    # Windows PowerShell은 OS 인증서 저장소를 사용하지만 httpx의 기본 CA
    # 저장소는 회사망 HTTPS 검사 인증서를 모를 수 있다. 검증을 끄지 않고
    # truststore를 통해 운영체제의 신뢰 체인을 그대로 사용한다.
    ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_options: dict[str, Any] = {
        "api_key": settings.api_key,
        "timeout": settings.timeout,
        "max_retries": settings.max_retries,
        "http_client": httpx.Client(verify=ssl_context),
    }
    if settings.base_url:
        client_options["base_url"] = settings.base_url
    return OpenAI(
        **client_options,
    )


def _provider_label(provider: str) -> str:
    return {
        "openai": "OpenAI",
        "openrouter": "OpenRouter",
        "openai-compatible": "OpenAI 호환 API",
    }.get(provider.lower(), provider)


def _exception_detail(exc: Exception, api_key: str) -> str:
    """SDK 래퍼 아래의 TLS·DNS 원인까지 키를 가린 상태로 표시한다."""
    details: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).replace(api_key, "<redacted>") if api_key else str(current)
        item = f"{type(current).__name__}: {message}"
        if item not in details:
            details.append(item)
        current = current.__cause__ or current.__context__
    return " -> ".join(details) or type(exc).__name__


def _law_facts(
    law: LawChange,
    *,
    batch_date: str = "",
    promulgation_date: str = "",
    promulgation_no: str = "",
) -> dict[str, Any]:
    enforce_status = "확인 필요"
    batch = _parse_iso_date(batch_date)
    enforce = _parse_iso_date(law.enforce_date)
    if batch and enforce:
        enforce_status = "시행 전" if enforce > batch else "시행일 도래"
    return {
        "batch_date": batch_date,
        "detection_semantics": "이번 배치에서 DB에 없던 최신 버전을 신규 감지",
        "law_id": law.law_id,
        "law_type": law.law_type,
        "law_name": law.law_name,
        "internal_name": law.internal_name,
        "old_serial_no": law.old_serial_no,
        "new_serial_no": law.new_serial_no,
        "promulgation_date": promulgation_date,
        "promulgation_no": promulgation_no,
        "enforce_date": law.enforce_date,
        "enforce_status_at_batch": enforce_status,
        "promulgated_version": True,
        "revision_type": law.revision_type,
        "official_revision_reason": law.revision_reason,
        "articles": [
            {
                "location": article.location_label,
                "change_type": article.change_type,
                "old_text": article.old_text,
                "new_text": article.new_text,
                "match_status": article.match_status,
            }
            for article in law.articles
        ],
        "structural_expansions": [
            {
                "article_label": expansion.article_label,
                "old_text_context": expansion.old_text,
                "new_items": [item.model_dump() for item in expansion.new_items],
            }
            for expansion in law.structural_expansions
        ],
        "unchanged_clauses": law.unchanged_clauses,
    }


_ARTICLE_REF_RE = re.compile(r"제\d+조(?:의\d+)?")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_CJK_CHAR_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_PRE_PROMULGATION_RE = re.compile(r"(개정안|법안|발의|입법예고)")
_WEEKLY_EVENT_RE = re.compile(r"(이번\s*주|금주|최근\s*(?:1|일)\s*주)")
# "시행됩니다"는 법령 문서에서 과거 날짜와 함께 현재 시행 상태를 설명하는
# 관용 표현으로도 쓰인다(예: "2025년 10월 1일부터 시행됩니다"). 이를 미래
# 시제로 단정하면 정상 요약을 거짓 양성으로 폐기한다. 미래임이 명시된
# "시행 예정/시행될 예정"만 차단한다.
_FUTURE_ENFORCEMENT_RE = re.compile(r"시행(?:될)?\s*예정")


def _validate_summary_locations(law: LawChange, summary: _LawSummaryContent) -> None:
    """LLM이 입력에 없는 조문 번호를 핵심 변경 위치로 만들지 못하게 한다."""
    allowed = {
        match.group(0)
        for label in [
            *(article.article_label for article in law.articles),
            *(expansion.article_label for expansion in law.structural_expansions),
        ]
        if (match := _ARTICLE_REF_RE.search(label or ""))
    }
    for item in summary.key_changes:
        match = _ARTICLE_REF_RE.search(item)
        if match is None:
            raise LLMSummaryError(
                f"{law.law_name} 요약의 핵심 변경사항에 조문 위치가 없습니다: {item}"
            )
        if match.group(0) not in allowed:
            raise LLMSummaryError(
                f"{law.law_name} 요약이 입력에 없는 조문을 사용했습니다: {match.group(0)}"
            )


def _validate_law_summary(
    law: LawChange,
    summary: _LawSummaryContent,
    facts: dict[str, Any],
) -> None:
    """법령별 요약의 위치·공포 상태·연도·시행 시제를 입력 사실과 대조한다."""
    _validate_summary_locations(law, summary)
    text = _law_summary_text(summary)
    if match := _PRE_PROMULGATION_RE.search(text):
        raise LLMSummaryError(
            f"{law.law_name} 요약이 공포된 법령을 공포 전 단계로 표현했습니다: {match.group(0)}"
        )
    _validate_output_years(law.law_name, text, facts)
    _validate_output_cjk(law.law_name, text, facts)

    batch = _parse_iso_date(str(facts.get("batch_date") or ""))
    enforce = _parse_iso_date(law.enforce_date)
    if batch and enforce and enforce <= batch and _FUTURE_ENFORCEMENT_RE.search(text):
        raise LLMSummaryError(
            f"{law.law_name} 요약이 이미 도래한 시행일을 미래 시제로 표현했습니다."
        )


def _validate_executive_summary(
    contract: WeeklyContract,
    summary: _ExecutiveSummaryContent,
) -> None:
    """종합 요약이 주간 개정으로 오인되거나 새 날짜를 만들지 못하게 한다."""
    text = summary.executive_summary.strip()
    if not text.startswith("이번 배치에서 신규 감지된 법령 버전은"):
        raise LLMSummaryError(
            "종합 요약이 배치 신규감지 의미로 시작하지 않습니다."
        )
    if match := _WEEKLY_EVENT_RE.search(text):
        raise LLMSummaryError(
            f"종합 요약이 보고기간을 실제 개정기간으로 표현했습니다: {match.group(0)}"
        )
    if match := _PRE_PROMULGATION_RE.search(text):
        raise LLMSummaryError(
            f"종합 요약이 공포된 법령을 공포 전 단계로 표현했습니다: {match.group(0)}"
        )
    source_facts = contract.model_dump(
        mode="json",
        exclude={"llm_summary", "verification"},
    )
    _validate_output_years("종합", text, source_facts)
    _validate_output_cjk("종합", text, source_facts)

    batch = _parse_iso_date(contract.batch_date)
    enforce_dates = [
        parsed
        for group in contract.amendment_groups
        for law in group.laws
        if (parsed := _parse_iso_date(law.enforce_date))
    ]
    if (
        batch
        and enforce_dates
        and all(enforce <= batch for enforce in enforce_dates)
        and _FUTURE_ENFORCEMENT_RE.search(text)
    ):
        raise LLMSummaryError("종합 요약이 이미 도래한 시행일을 미래 시제로 표현했습니다.")


def _law_summary_text(summary: _LawSummaryContent) -> str:
    return " ".join(
        [
            summary.headline,
            summary.summary,
            *summary.key_changes,
            summary.operational_impact,
            *summary.review_points,
        ]
    )


def _validate_output_years(label: str, output_text: str, facts: Any) -> None:
    allowed = set(_YEAR_RE.findall(json.dumps(facts, ensure_ascii=False)))
    generated = set(_YEAR_RE.findall(output_text))
    unexpected = sorted(generated - allowed)
    if unexpected:
        raise LLMSummaryError(
            f"{label} 요약이 입력에 없는 연도를 사용했습니다: {', '.join(unexpected)}"
        )


def _validate_output_cjk(label: str, output_text: str, facts: Any) -> None:
    """입력 원문에 없는 한자ㆍ중국어 문자가 생성 문장에 섞이는 것을 막는다."""
    allowed = set(_CJK_CHAR_RE.findall(json.dumps(facts, ensure_ascii=False)))
    generated = set(_CJK_CHAR_RE.findall(output_text))
    unexpected = sorted(generated - allowed)
    if unexpected:
        raise LLMSummaryError(
            f"{label} 요약이 입력에 없는 한자 문자를 사용했습니다: "
            f"{', '.join(unexpected)}"
        )


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )
