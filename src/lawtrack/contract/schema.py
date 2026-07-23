"""LLM 팀 전달용 산출물 스키마.

이 파일이 우리 팀과 LLM팀 사이의 '계약'이다. Pydantic 모델로 정의해
내보내기 직전에 자기 검증을 거치게 한다 — 필드 하나가 빠진 채로
넘어가면 LLM팀 쪽에서 원인 모를 오류가 나는데, 그건 우리 책임이다.

설계 원칙 (프로젝트 전체에서 지켜온 것):
    LLM 에게는 이미 확정된 사실만 준다. "무엇이 바뀌었는지 찾아라"는
    시키지 않는다 — 그건 <P> 태그와 6가드가 이미 결정론적으로 확정한
    것이다. LLM 의 역할은 이 사실들을 문장으로 다듬는 것뿐이다.

구조가 amendment_groups(공포번호 그룹) 를 최상위로 두는 이유:
    실측: 공포번호 하나로 5개+ 법이 동시 개정된다. 법을 평평하게
    나열하면 LLM 이 "정부조직 개편 일괄정비" 같은 맥락을 못 잡고
    무관해 보이는 항목들을 따로따로 서술한다.

unresolved 를 절대 비우지 않는 이유:
    6가드로 위치 확정에 실패한 것을 조용히 빼면, LLM 이 "확정 안 됨"을
    모른 채 문장을 만들 위험(환각)이 생긴다. 실패는 실패로 명시한다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Period(BaseModel):
    from_date: str = Field(..., description="YYYY-MM-DD")
    to_date: str = Field(..., description="YYYY-MM-DD")


class ArticleDiffItem(BaseModel):
    """조문 단위 변경 사실 — 이미 확정된 것만 담는다."""

    article_label: str = Field(..., description="예: 제6조의2")
    clause_no: str = ""
    item_label: str = ""
    subitem_label: str = ""
    change_type: str = Field(..., description="개정 | 신설 | 삭제 | 미상")
    old_text: str = ""
    new_text: str = ""
    match_status: str = Field(
        ...,
        description=(
            "성공 | 삭제(위치탐색제외) | "
            "구조확장(구법미분리)(이 항/호/목은 개정으로 새로 생긴 구조라 "
            "구법엔 대응하는 조각이 없음 — old_text는 그 항/조 전체의 "
            "개정 전 문장(참고 맥락)일 뿐, 이 특정 항목의 개정 전 내용이 "
            "아니므로 신뢰하지 말 것) | "
            "위치재배치의심(같은 조문 안의 신설 문장 중 old_text와 현재 "
            "new_text보다 구문상 훨씬 가까운 문장이 확인되어, 법제처 원본 "
            "신구조문대비표가 신/구를 순서 기준으로 잘못 대응시켰을 가능성이 "
            "높음 — old_text를 '개정 전 같은 조항'으로 신뢰하지 말 것)"
        ),
    )

    @property
    def location_label(self) -> str:
        parts = [self.article_label, self.clause_no, self.item_label, self.subitem_label]
        return "".join(p for p in parts if p)


class ExpandedItem(BaseModel):
    """구조확장(StructuralExpansion)으로 새로 생긴 위치 하나.

    ★ 실측(2026-07-19, 전자정부법 제56조의3①~④): 구조확장은 "한 항 안에
    호/목이 새로 생기는" 경우만이 아니라 "조문 하나(항 구분조차 없던
    통짜 조문)가 통째로 여러 개의 새 항(①②③④)으로 재작성"되는 경우도
    있다 — 이때는 새로 생긴 위치들의 clause_no 자체가 서로 다르므로,
    clause_no를 그룹(StructuralExpansion) 레벨이 아니라 이 항목 레벨에
    둬야 각 위치를 정확히 표시할 수 있다."""

    clause_no: str = ""
    item_label: str = ""
    subitem_label: str = ""
    text: str = Field(..., description="이 위치의 개정 후(new) 문장 — 정확함")


class StructuralExpansion(BaseModel):
    """★ 설계(2026-07-19, LLM팀 산출물 리뷰): 구법엔 없던 항/호/목 구조가
    개정으로 새로 생긴 경우(예: 구법 "① 통짜 문장"이 신법에서 "① + 1.~5.
    호 목록"으로 재작성됨, 또는 항 구분조차 없던 조문 하나가 새 항 여러
    개로 재작성됨), old_text 1개에 new 위치가 여러 개 딸린다. 처음엔 이
    경우도 articles[] 안에 old_text를 복제해 넣고
    match_status="구조확장(구법미분리)"로만 표시했는데, "행 하나 = 위치
    하나의 1:1 대응"이라는 articles[]의 기본 전제가 이 케이스에서만
    깨지다 보니 라벨을 아무리 명확히 붙여도 헷갈린다는 지적을 받았다.
    그래서 이 케이스는 articles[]에서 완전히 빼내 별도 배열로 분리한다
    — articles[]는 항상 깨끗한 1:1만 담고, "이건 그룹이다"가 배열
    자체로 드러나게 하는 것이 목적. old_text는 참고 맥락 그 자체이므로
    (구법에 이 세부 위치가 없었으니 특정 new_item 하나에 대응하는
    개정 전 문장은 존재하지 않음) 안내문 접두어가 필요 없다 — 배열
    이름과 구조가 이미 그 의미를 담고 있다."""

    article_label: str
    old_text: str = Field(
        ..., description="구법의 통짜 원문(참고 맥락) — new_items 각각에 정밀 대응하지 않음"
    )
    new_items: list[ExpandedItem] = Field(default_factory=list)


class LawChange(BaseModel):
    """법령/행정규칙 1건의 개정 정보."""

    law_id: str
    law_type: str = Field(..., description="법률 | 시행령 | 시행규칙 | 행정규칙")
    law_name: str
    internal_name: str = ""
    dept_codes: list[str] = Field(default_factory=list)
    old_serial_no: str = ""
    new_serial_no: str
    enforce_date: str = ""
    revision_type: str = ""
    revision_reason: str = Field(
        "", description="법제처 공식 개정이유 — LLM 이 추론할 필요 없게 함"
    )
    source_url: str = ""
    articles: list[ArticleDiffItem] = Field(default_factory=list)
    structural_expansions: list[StructuralExpansion] = Field(
        default_factory=list,
        description=(
            "구법엔 없던 항/호/목 구조가 개정으로 새로 생긴 그룹 — 여기 담긴 "
            "old_text는 그룹 전체의 참고 맥락이지 new_items 각각의 정밀한 "
            "개정 전 문장이 아니다. articles[]에는 이런 1:N 케이스가 절대 "
            "섞이지 않는다(항상 1:1만 담김)."
        ),
    )
    unchanged_clauses: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "개정된 조문 중 이번에 안 바뀐 항/호(현행) 라벨. 예: "
            '{"제34조": ["①","②","③"]}. 법령은 법제처 항제개정유형 공식 '
            "필드 기준(항 라벨만), 행정규칙은 신구법 비교의 "
            "\"(생략)/(현행과 같음)\" 스킵 표시 기준(항 또는 호 라벨) — "
            "두 경우 모두 법제처 원본이 준 사실이며 추론이 아니다. 스킵 "
            "표시가 없거나 해당 조문이 이번에 CHANGED로 감지되지 않았으면 "
            "비어 있다."
        ),
    )

    @field_validator("new_serial_no")
    @classmethod
    def _serial_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("new_serial_no 는 비어 있을 수 없음")
        return v


class AmendmentGroup(BaseModel):
    """같은 공포번호로 묶인 개정 이벤트."""

    group_id: str
    promulgation_no: str = ""
    promulgation_date: str = ""
    revision_type: str = ""
    affected_law_ids: list[str] = Field(default_factory=list)
    laws: list[LawChange] = Field(default_factory=list)

    @property
    def is_chained(self) -> bool:
        return len(self.affected_law_ids) > 1


class UnresolvedItem(BaseModel):
    """★ 6가드로 위치 확정에 실패한 건. 절대 조용히 빠지지 않는다."""

    law_id: str
    law_name: str
    new_serial_no: str
    reason: str = Field(..., description="0건실패 | 중복실패")
    detail: str = ""
    source_url: str = ""
    guards_tried: list[str] = Field(default_factory=list)


class NoComparisonItem(BaseModel):
    """개정은 감지됐으나 신구법 대비 자체가 불가능한 건 (제정 등)."""

    law_id: str
    law_name: str
    new_serial_no: str
    reason: str = Field(..., description="법령 제정 | 행정규칙 제정 | 기타")
    note: str = "신구법 대비표 없음 — 원문 링크 참조"
    source_url: str = ""


class LawLLMSummary(BaseModel):
    """LLM이 확정된 변경 사실만 바탕으로 작성한 법령별 업무용 요약."""

    model_config = ConfigDict(extra="forbid")

    law_id: str
    new_serial_no: str
    headline: str
    summary: str
    key_changes: list[str] = Field(default_factory=list)
    operational_impact: str = ""
    review_points: list[str] = Field(default_factory=list)


class LLMSummary(BaseModel):
    """주간 계약에 함께 보존되는 LLM 요약과 생성 메타데이터."""

    provider: str = "openai"
    model: str
    generated_at: str
    executive_summary: str
    law_summaries: list[LawLLMSummary] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


class VerificationIssue(BaseModel):
    """원본 또는 LLM 요약 검증에서 발견된 단일 문제."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["WARNING", "ERROR"]
    category: Literal["SOURCE", "SUMMARY", "SYSTEM"]
    code: str
    law_id: str = ""
    new_serial_no: str = ""
    location: str = ""
    field: str = ""
    claim: str = ""
    evidence: str = ""
    reason: str


class VerificationReport(BaseModel):
    """코드 기반 원본 검사와 독립 LLM 검증 에이전트의 통합 결과."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["PASS", "WARN", "FAIL"]
    source_integrity: Literal["PASS", "WARN", "FAIL"]
    summary_grounding: Literal["PASS", "WARN", "FAIL", "NOT_RUN"] = "NOT_RUN"
    provider: str = ""
    model: str = ""
    verified_at: str
    source_sha256: str
    summary_sha256: str = ""
    expected_version_count: int = 0
    contract_version_count: int = 0
    checked_law_count: int = 0
    issues: list[VerificationIssue] = Field(default_factory=list)
    missing_locations: list[str] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


class WeeklyContract(BaseModel):
    """주간 산출물 최상위 스키마. LLM팀이 받는 실제 구조."""

    contract_version: str = "1.0"
    batch_date: str = Field(..., description="YYYY-MM-DD")
    period: Period
    amendment_groups: list[AmendmentGroup] = Field(default_factory=list)
    unresolved: list[UnresolvedItem] = Field(default_factory=list)
    no_comparison: list[NoComparisonItem] = Field(default_factory=list)
    llm_summary: LLMSummary | None = None
    verification: VerificationReport | None = None

    @property
    def total_law_count(self) -> int:
        return sum(len(g.laws) for g in self.amendment_groups)

    @property
    def total_article_count(self) -> int:
        return sum(len(law.articles) for g in self.amendment_groups for law in g.laws)

    @property
    def total_structural_expansion_count(self) -> int:
        return sum(
            len(law.structural_expansions) for g in self.amendment_groups for law in g.laws
        )

    def summary(self) -> str:
        return (
            f"{self.batch_date} 배치: 그룹 {len(self.amendment_groups)}개, "
            f"법 {self.total_law_count}건, 조문변경 {self.total_article_count}건, "
            f"구조확장그룹 {self.total_structural_expansion_count}건, "
            f"미확정 {len(self.unresolved)}건, 비교불가 {len(self.no_comparison)}건"
        )
