"""네트워크 없이 OpenAI 구조화 요약 계약을 검증한다."""

from datetime import datetime
from types import SimpleNamespace

from lawtrack.config import OpenAISettings
from lawtrack.contract.schema import (
    AmendmentGroup,
    ArticleDiffItem,
    LawChange,
    Period,
    VerificationIssue,
    VerificationReport,
    WeeklyContract,
)
from lawtrack.llm.openai_summary import (
    SYSTEM_PROMPT,
    LLMSummaryError,
    _LawSummaryContent,
    _ExecutiveSummaryContent,
    _exception_detail,
    _law_facts,
    _new_client,
    _validate_executive_summary,
    _validate_law_summary,
    _validate_summary_locations,
    summarize_contract,
)


class _FakeResponses:
    def __init__(self):
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        schema = kwargs["text_format"]
        if "executive_summary" in schema.model_fields:
            parsed = schema(
                executive_summary=(
                    "이번 배치에서 신규 감지된 법령 버전은 1건입니다. "
                    "테스트법의 기관 명칭이 정비되었습니다."
                )
            )
        else:
            parsed = schema(
                headline="기관 명칭 정비",
                summary="정부조직 개편에 따라 조문에 사용된 기관 명칭을 정비했습니다.",
                key_changes=["제1조의 종전 기관 명칭을 새 명칭으로 변경"],
                operational_impact="관련 서식과 내부 안내문에 사용된 기관 명칭 확인 필요",
                review_points=["시행일 이후 문서의 기관 명칭 확인"],
            )
        return SimpleNamespace(
            output_parsed=parsed,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


class _ChineseContaminationThenValidResponses(_FakeResponses):
    def __init__(self):
        super().__init__()
        self.law_attempts = 0

    def parse(self, **kwargs):
        schema = kwargs["text_format"]
        if "executive_summary" not in schema.model_fields:
            self.calls.append(kwargs)
            self.law_attempts += 1
            parsed = schema(
                headline="기관 명칭 정비",
                summary=(
                    "개정内容은 기관 명칭을 정비했습니다."
                    if self.law_attempts == 1
                    else "개정 내용은 기관 명칭을 정비했습니다."
                ),
                key_changes=["제1조의 종전 기관 명칭을 새 명칭으로 변경"],
                operational_impact="담당 부서의 원문 검토 필요",
                review_points=[],
            )
            return SimpleNamespace(
                output_parsed=parsed,
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )
        return super().parse(**kwargs)


def _contract():
    return WeeklyContract(
        batch_date="2026-07-21",
        period=Period(from_date="2026-07-14", to_date="2026-07-21"),
        amendment_groups=[
            AmendmentGroup(
                group_id="single-1",
                promulgation_no="12345",
                promulgation_date="2026-07-19",
                laws=[
                    LawChange(
                        law_id="001",
                        law_type="법률",
                        law_name="테스트법",
                        new_serial_no="200",
                        enforce_date="2026-07-20",
                        revision_reason="정부조직 개편에 따른 기관 명칭을 정비함.",
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


def test_structured_openrouter_summary_is_saved_in_contract():
    responses = _FakeResponses()
    client = SimpleNamespace(responses=responses)
    settings = OpenAISettings(
        api_key="secret",
        model="openai/gpt-test",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        enabled=True,
    )

    result = summarize_contract(
        _contract(),
        settings,
        client=client,
        generated_at=datetime(2026, 7, 21, 10, 30),
    )

    assert len(responses.calls) == 2
    assert result.llm_summary is not None
    assert result.llm_summary.provider == "openrouter"
    assert result.llm_summary.model == "openai/gpt-test"
    assert result.llm_summary.law_summaries[0].law_id == "001"
    assert result.llm_summary.law_summaries[0].headline == "기관 명칭 정비"
    assert result.llm_summary.input_tokens == 20
    assert result.llm_summary.output_tokens == 10
    assert "secret" not in result.model_dump_json()


def test_verified_error_is_sent_to_single_correction_attempt():
    first_responses = _FakeResponses()
    settings = OpenAISettings(
        api_key="secret",
        model="openai/gpt-test",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        enabled=True,
    )
    summarized = summarize_contract(
        _contract(),
        settings,
        client=SimpleNamespace(responses=first_responses),
    )
    report = VerificationReport(
        status="FAIL",
        source_integrity="PASS",
        summary_grounding="FAIL",
        verified_at="2026-07-21T11:00:00",
        source_sha256="a" * 64,
        summary_sha256="b" * 64,
        issues=[
            VerificationIssue(
                severity="ERROR",
                category="SUMMARY",
                code="UNSUPPORTED_CLAIM",
                law_id="001",
                new_serial_no="200",
                field="summary",
                claim="잘못된 주장",
                evidence="새 기관",
                reason="요약이 원문에 없는 의무를 추가했습니다.",
            )
        ],
    )
    retry_responses = _FakeResponses()

    summarize_contract(
        summarized,
        settings,
        client=SimpleNamespace(responses=retry_responses),
        verification_feedback=report,
    )

    law_prompt = retry_responses.calls[0]["input"][1]["content"]
    assert "correction_context" in law_prompt
    assert "요약이 원문에 없는 의무를 추가했습니다." in law_prompt
    assert "previous_summary" in law_prompt


def test_invalid_chinese_output_is_automatically_rewritten_once():
    responses = _ChineseContaminationThenValidResponses()
    settings = OpenAISettings(
        api_key="secret",
        model="openai/gpt-test",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        enabled=True,
    )

    result = summarize_contract(
        _contract(),
        settings,
        client=SimpleNamespace(responses=responses),
    )

    assert len(responses.calls) == 3
    assert "内容" not in result.llm_summary.law_summaries[0].summary
    retry_prompt = responses.calls[1]["input"][1]["content"]
    assert "output_validation_feedback" in retry_prompt
    assert "입력에 없는 한자 문자" in retry_prompt


def test_openrouter_base_url_is_passed_to_openai_sdk():
    settings = OpenAISettings(
        api_key="test-key",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        enabled=True,
    )

    client = _new_client(settings)
    try:
        assert str(client.base_url) == "https://openrouter.ai/api/v1/"
    finally:
        client.close()


def test_nested_connection_cause_is_visible_without_api_key():
    try:
        try:
            raise RuntimeError("TLS failed for secret-key")
        except RuntimeError as cause:
            raise ConnectionError("Connection error") from cause
    except ConnectionError as exc:
        detail = _exception_detail(exc, "secret-key")

    assert "ConnectionError: Connection error" in detail
    assert "RuntimeError: TLS failed for <redacted>" in detail
    assert "secret-key" not in detail


def test_summary_rejects_article_number_not_present_in_input():
    law = _contract().amendment_groups[0].laws[0]
    summary = _LawSummaryContent(
        headline="잘못된 위치",
        summary="입력에 없는 조문을 사용했습니다.",
        key_changes=["제76조: 잘못된 위치"],
        operational_impact="담당 부서의 원문 검토 필요",
        review_points=[],
    )

    try:
        _validate_summary_locations(law, summary)
    except LLMSummaryError as exc:
        assert "입력에 없는 조문" in str(exc)
    else:
        raise AssertionError("입력에 없는 조문 번호를 허용했습니다.")


def test_prompt_warns_that_other_law_revision_reason_has_different_scope():
    assert "타법개정" in SYSTEM_PROMPT
    assert "다른 법률의 제정ㆍ개정 이유" in SYSTEM_PROMPT
    assert "이번 배치에서 신규 감지된 법령 버전" in SYSTEM_PROMPT
    assert "개정안" in SYSTEM_PROMPT


def test_law_summary_rejects_bill_stage_and_invented_year():
    contract = _contract()
    law = contract.amendment_groups[0].laws[0]
    facts = _law_facts(
        law,
        batch_date=contract.batch_date,
        promulgation_date="2026-07-19",
        promulgation_no="12345",
    )
    summary = _LawSummaryContent(
        headline="테스트법 일부개정안(2023)",
        summary="2023년에 발의된 개정안입니다.",
        key_changes=["제1조: 기관 명칭 변경"],
        operational_impact="담당 부서의 원문 검토 필요",
        review_points=[],
    )

    try:
        _validate_law_summary(law, summary, facts)
    except LLMSummaryError as exc:
        assert "공포 전 단계" in str(exc)
    else:
        raise AssertionError("공포된 법령을 개정안·발의로 표현한 요약을 허용했습니다.")


def test_law_summary_rejects_future_tense_for_past_enforcement_date():
    contract = _contract()
    law = contract.amendment_groups[0].laws[0]
    facts = _law_facts(
        law,
        batch_date=contract.batch_date,
        promulgation_date="2026-07-19",
        promulgation_no="12345",
    )
    summary = _LawSummaryContent(
        headline="기관 명칭 정비",
        summary="이 법은 2026년 7월 20일 시행될 예정입니다.",
        key_changes=["제1조: 기관 명칭 변경"],
        operational_impact="담당 부서의 원문 검토 필요",
        review_points=[],
    )

    try:
        _validate_law_summary(law, summary, facts)
    except LLMSummaryError as exc:
        assert "미래 시제" in str(exc)
    else:
        raise AssertionError("이미 도래한 시행일을 미래 시제로 표현한 요약을 허용했습니다.")


def test_law_summary_allows_standard_enforcement_wording_for_past_date():
    """'시행됩니다'는 과거 시행일을 설명하는 공문서 표현일 수도 있다."""
    contract = _contract()
    law = contract.amendment_groups[0].laws[0]
    facts = _law_facts(
        law,
        batch_date=contract.batch_date,
        promulgation_date="2026-07-19",
        promulgation_no="12345",
    )
    summary = _LawSummaryContent(
        headline="기관 명칭 정비",
        summary="이 법령은 2026년 7월 20일부터 시행됩니다.",
        key_changes=["제1조 기관 명칭 변경"],
        operational_impact="담당 부서의 원문 검토 필요",
        review_points=[],
    )

    _validate_law_summary(law, summary, facts)


def test_executive_summary_rejects_weekly_event_semantics():
    summary = _ExecutiveSummaryContent(
        executive_summary="이번 주에는 테스트법 1건이 개정되었습니다."
    )

    try:
        _validate_executive_summary(_contract(), summary)
    except LLMSummaryError as exc:
        assert "배치 신규감지" in str(exc)
    else:
        raise AssertionError("보고기간을 실제 개정기간으로 표현한 종합 요약을 허용했습니다.")
