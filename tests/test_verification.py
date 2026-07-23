"""법령 원본 무결성 검사와 독립 요약 검증 에이전트 테스트."""

from datetime import datetime
from types import SimpleNamespace

from lawtrack.config import OpenAISettings, VerificationSettings
from lawtrack.contract.schema import (
    AmendmentGroup,
    ArticleDiffItem,
    LLMSummary,
    LawChange,
    LawLLMSummary,
    Period,
    WeeklyContract,
)
from lawtrack.llm.verifier import (
    _AuditIssue,
    _convert_issues,
    verify_summary,
)
from lawtrack.verify import verify_source_integrity


def _contract(*, source_url: str | None = None) -> WeeklyContract:
    return WeeklyContract(
        batch_date="2026-07-23",
        period=Period(from_date="2026-07-16", to_date="2026-07-23"),
        amendment_groups=[
            AmendmentGroup(
                group_id="single-001-200",
                promulgation_no="12345",
                promulgation_date="2026-07-20",
                affected_law_ids=["001"],
                laws=[
                    LawChange(
                        law_id="001",
                        law_type="법률",
                        law_name="테스트법",
                        old_serial_no="199",
                        new_serial_no="200",
                        enforce_date="2026-07-22",
                        revision_type="일부개정",
                        source_url=source_url or (
                            "https://www.law.go.kr/DRF/lawService.do?"
                            "target=law&MST=200&type=HTML"
                        ),
                        articles=[
                            ArticleDiffItem(
                                article_label="제1조",
                                change_type="개정",
                                old_text="종전 기관",
                                new_text="새 기관",
                                match_status="성공",
                            )
                        ],
                    )
                ],
            )
        ],
    )


def _with_summary(contract: WeeklyContract) -> WeeklyContract:
    return contract.model_copy(update={
        "llm_summary": LLMSummary(
            provider="openrouter",
            model="openai/gpt-4o-mini",
            generated_at="2026-07-23T12:00:00",
            executive_summary=(
                "이번 배치에서 신규 감지된 법령 버전은 1건입니다. "
                "테스트법의 기관 명칭이 정비되었습니다."
            ),
            law_summaries=[
                LawLLMSummary(
                    law_id="001",
                    new_serial_no="200",
                    headline="기관 명칭 정비",
                    summary="제1조의 종전 기관 명칭이 새 기관으로 변경되었습니다.",
                    key_changes=["제1조 기관 명칭 변경"],
                    operational_impact="담당 부서의 원문 검토 필요",
                    review_points=[],
                )
            ],
        )
    })


class _PassingResponses:
    def parse(self, **kwargs):
        schema = kwargs["text_format"]
        if "missing_locations" in schema.model_fields:
            parsed = schema(status="PASS", issues=[], missing_locations=[])
        else:
            parsed = schema(status="PASS", issues=[])
        return SimpleNamespace(
            output_parsed=parsed,
            usage=SimpleNamespace(input_tokens=11, output_tokens=3),
        )


class _FailingResponses:
    def __init__(self):
        self.call_count = 0

    def parse(self, **kwargs):
        self.call_count += 1
        schema = kwargs["text_format"]
        if "missing_locations" in schema.model_fields:
            parsed = schema(
                status="FAIL",
                issues=[
                    _AuditIssue(
                        issue_type="UNSUPPORTED",
                        severity="ERROR",
                        field="summary",
                        location="제1조",
                        claim="제1조의 종전 기관 명칭이 새 기관으로 변경되었습니다.",
                        evidence="새 기관",
                        reason="요약이 원문에 없는 의무를 포함해 원문과 다릅니다.",
                    )
                ],
                missing_locations=[],
            )
        else:
            parsed = schema(status="PASS", issues=[])
        return SimpleNamespace(
            output_parsed=parsed,
            usage=SimpleNamespace(input_tokens=7, output_tokens=2),
        )


def _settings() -> tuple[OpenAISettings, VerificationSettings]:
    openai = OpenAISettings(
        api_key="secret",
        model="openai/writer",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        enabled=True,
    )
    verification = VerificationSettings(
        enabled=True,
        required=True,
        fail_closed=True,
        model="openai/verifier",
    )
    return openai, verification


def test_source_integrity_passes_when_detected_versions_match_contract():
    report = verify_source_integrity(
        _contract(),
        expected_versions={("001", "200")},
        verified_at=datetime(2026, 7, 23, 13, 0),
    )

    assert report.status == "PASS"
    assert report.source_integrity == "PASS"
    assert report.expected_version_count == 1
    assert report.contract_version_count == 1
    assert len(report.source_sha256) == 64
    assert report.issues == []


def test_source_integrity_rejects_missing_version_and_secret_url():
    contract = _contract(
        source_url=(
            "https://www.law.go.kr/DRF/lawService.do?"
            "OC=top-secret&target=law&MST=200&type=HTML"
        )
    )
    report = verify_source_integrity(
        contract,
        expected_versions={("001", "200"), ("002", "300")},
        secrets=("top-secret",),
    )

    assert report.status == "FAIL"
    assert {issue.code for issue in report.issues} >= {
        "EXPECTED_VERSION_MISSING",
        "SECRET_IN_SOURCE_URL",
    }


def test_source_integrity_marks_failed_watchlist_item_and_redacts_secret():
    report = verify_source_integrity(
        _contract(),
        expected_versions={("001", "200")},
        processing_errors=[
            ("002", "실패법", "request failed: api-key-value"),
        ],
        secrets=("api-key-value",),
    )

    assert report.status == "FAIL"
    issue = next(item for item in report.issues if item.code == "BATCH_ITEM_FAILED")
    assert "<redacted>" in issue.reason
    assert "api-key-value" not in report.model_dump_json()


def test_independent_verifier_passes_grounded_summary():
    contract = _with_summary(_contract())
    source_report = verify_source_integrity(
        contract,
        expected_versions={("001", "200")},
    )
    openai, verification = _settings()

    report = verify_summary(
        contract,
        openai,
        verification,
        source_report,
        client=SimpleNamespace(responses=_PassingResponses()),
        verified_at=datetime(2026, 7, 23, 14, 0),
    )

    assert report.status == "PASS"
    assert report.summary_grounding == "PASS"
    assert report.model == "openai/verifier"
    assert report.checked_law_count == 1
    assert report.input_tokens == 22
    assert report.output_tokens == 6
    assert len(report.summary_sha256) == 64


def test_independent_verifier_fails_unsupported_claim():
    contract = _with_summary(_contract())
    source_report = verify_source_integrity(
        contract,
        expected_versions={("001", "200")},
    )
    openai, verification = _settings()

    report = verify_summary(
        contract,
        openai,
        verification,
        source_report,
        client=SimpleNamespace(responses=_FailingResponses()),
    )

    assert report.status == "FAIL"
    assert report.summary_grounding == "FAIL"
    assert report.issues[-1].category == "SUMMARY"
    assert report.issues[-1].law_id == "001"
    assert report.issues[-1].evidence == "새 기관"


class _UngroundedVerifierResponses:
    def parse(self, **kwargs):
        schema = kwargs["text_format"]
        if "missing_locations" in schema.model_fields:
            parsed = schema(
                status="FAIL",
                issues=[
                    _AuditIssue(
                        issue_type="UNSUPPORTED",
                        severity="ERROR",
                        field="summary",
                        claim="입력에 없는 영문 법령명",
                        evidence="입력에 없는 가짜 근거",
                        reason="검증기 자체가 근거를 만들었습니다.",
                    )
                ],
                missing_locations=[],
            )
        else:
            parsed = schema(status="PASS", issues=[])
        return SimpleNamespace(
            output_parsed=parsed,
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )


def test_verifier_hallucination_is_warning_not_summary_failure():
    contract = _with_summary(_contract())
    source_report = verify_source_integrity(
        contract,
        expected_versions={("001", "200")},
    )
    openai, verification = _settings()

    report = verify_summary(
        contract,
        openai,
        verification,
        source_report,
        client=SimpleNamespace(responses=_UngroundedVerifierResponses()),
    )

    assert report.status == "WARN"
    assert report.summary_grounding == "WARN"
    assert all(issue.severity != "ERROR" for issue in report.issues)
    assert any(
        issue.code in {
            "VERIFIER_UNSUPPORTED_FINDING",
            "VERIFIER_INVALID_WARNING_EVIDENCE",
        }
        for issue in report.issues
    )


def test_misclassified_omission_is_forced_to_warning():
    payload = {
        "source_facts": {"articles": [{"location": "제9조", "new_text": "새 기관"}]},
        "generated_summary": {"key_changes": ["제9조 기관 명칭 변경"]},
    }
    converted, invalid = _convert_issues(
        [
            _AuditIssue(
                issue_type="CONTRADICTION",
                severity="ERROR",
                field="key_changes",
                location="제9조",
                claim="제9조 기관 명칭 변경",
                evidence="새 기관",
                reason="제9조의 모든 관련 조문이 포함되지 않아 일부가 누락되었습니다.",
            )
        ],
        payload=payload,
        law_id="001",
        serial_no="200",
    )

    assert invalid == []
    assert converted[0].severity == "WARNING"
    assert converted[0].code == "SUMMARY_OMISSION"


def test_subjective_clarity_feedback_cannot_fail_summary():
    payload = {
        "source_facts": {
            "articles": [{"location": "제21조④", "new_text": "수의계약으로 구매할 수 있다."}]
        },
        "generated_summary": {
            "key_changes": ["제21조④ 수의계약 근거가 신설되었습니다."]
        },
    }
    converted, invalid = _convert_issues(
        [
            _AuditIssue(
                issue_type="CONTRADICTION",
                severity="ERROR",
                field="key_changes",
                location="제21조④",
                claim="제21조④ 수의계약 근거가 신설되었습니다.",
                evidence="수의계약으로 구매할 수 있다.",
                reason=(
                    "제21조④에 대한 설명이 다른 내용과 연계되지 않아 "
                    "명확한 설명이 부족하다."
                ),
            )
        ],
        payload=payload,
        law_id="001605",
        serial_no="286627",
    )

    assert invalid == []
    assert converted[0].severity == "WARNING"
    assert converted[0].code == "SUMMARY_QUALITY_NOTE"


def test_direct_source_contradiction_remains_error():
    payload = {
        "source_facts": {
            "articles": [{"location": "제1조", "new_text": "새 기관"}]
        },
        "generated_summary": {
            "key_changes": ["제1조 종전 기관을 유지합니다."]
        },
    }
    converted, invalid = _convert_issues(
        [
            _AuditIssue(
                issue_type="CONTRADICTION",
                severity="ERROR",
                field="key_changes",
                location="제1조",
                claim="제1조 종전 기관을 유지합니다.",
                evidence="새 기관",
                reason="요약 내용이 원문의 기관 변경 사실과 다릅니다.",
            )
        ],
        payload=payload,
        law_id="001",
        serial_no="200",
    )

    assert invalid == []
    assert converted[0].severity == "ERROR"
    assert converted[0].code == "CONTRADICTORY_CLAIM"
