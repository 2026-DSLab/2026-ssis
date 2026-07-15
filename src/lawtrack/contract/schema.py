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

from pydantic import BaseModel, Field, field_validator


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
    match_status: str = Field(..., description="성공 | 삭제(위치탐색제외)")

    @property
    def location_label(self) -> str:
        parts = [self.article_label, self.clause_no, self.item_label, self.subitem_label]
        return "".join(p for p in parts if p)


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


class WeeklyContract(BaseModel):
    """주간 산출물 최상위 스키마. LLM팀이 받는 실제 구조."""

    contract_version: str = "1.0"
    batch_date: str = Field(..., description="YYYY-MM-DD")
    period: Period
    amendment_groups: list[AmendmentGroup] = Field(default_factory=list)
    unresolved: list[UnresolvedItem] = Field(default_factory=list)
    no_comparison: list[NoComparisonItem] = Field(default_factory=list)

    @property
    def total_law_count(self) -> int:
        return sum(len(g.laws) for g in self.amendment_groups)

    @property
    def total_article_count(self) -> int:
        return sum(len(law.articles) for g in self.amendment_groups for law in g.laws)

    def summary(self) -> str:
        return (
            f"{self.batch_date} 배치: 그룹 {len(self.amendment_groups)}개, "
            f"법 {self.total_law_count}건, 조문변경 {self.total_article_count}건, "
            f"미확정 {len(self.unresolved)}건, 비교불가 {len(self.no_comparison)}건"
        )