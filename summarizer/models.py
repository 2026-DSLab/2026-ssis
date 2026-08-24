"""파이프라인 내부 자료구조.

계약(lawtrack.contract.schema)은 '입력' 스키마이고, 여기 있는 것들은
파이프라인 '내부' 및 '출력' 스키마임. 둘을 섞지 않음 — 계약은 앞 단계가
정한 형식이라 바꿀 수 없고, 이건 이 모듈이 자유롭게 바꿀 수 있음.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ArticleUnit:
    """1단계 에이전트의 입력 단위 — 조문 위치 하나.

    계약의 articles[] 와 structural_expansions[] 는 서로 다른 배열이지만
    (1:1 대응이냐 1:N 이냐의 차이), 요약 관점에서는 둘 다 "위치 하나 +
    개정 전/후 문장"으로 환원됨. 여기서 그 차이를 흡수해 1단계
    에이전트가 배열 종류를 신경 쓰지 않게 함.

    old_text_is_context 가 이 구조의 핵심임.
      True 면 old_text 는 "이 위치의 개정 전 문장"이 아니라 참고 맥락일
      뿐임. 프롬프트에서 반드시 그렇게 표시해야 함 — 빠뜨리면 LLM 이
      서로 대응하지 않는 두 문장을 나란히 놓고 "이렇게 바뀌었다"고
      환각함. 계약의 match_status 주석이 경고하는 바로 그 상황임.
    """

    law_id: str
    law_name: str
    location_label: str
    change_type: str
    old_text: str
    new_text: str
    match_status: str
    old_text_is_context: bool = False

    moved_from: str | None = None
    """번호가 밀려온 경우 원래 위치. 예: '①7.'

    change_type 이 '이동' / '이동후개정' 일 때만 채워짐. 매핑 단계가
    계약의 틀린 라벨을 교정하면서 넣음."""

    label_corrected: bool = False
    """계약의 change_type 을 매핑 결과로 바꿨는지 여부. 로그·추적용."""

    move_is_identical: bool = False
    """번호만 밀렸고 내용은 글자까지 동일함이 계산으로 확정된 경우.
    이때는 LLM 을 부르지 않고 요약문을 규칙으로 만듦 — 부를 이유가 없고,
    부르면 오히려 없던 변경을 지어낼 위험만 생김."""

    diff_parts: list[str] = field(default_factory=list)
    """제자리 개정에서 글자단위 비교로 뽑은 '바뀐 조각'. 예:
    ["'대한올림픽위원회' → '대한체육회'"]. LLM 은 원문 전체가 아니라
    이 조각만 보고 문장을 다듬으므로, 없던 변경을 지어낼 수 없음."""

    is_whole_replace: bool = False
    """개정 전/후가 서로 무관해 통째로 교체된 경우. diff 로 쪼개면 무의미한
    조각이 나오므로, 프롬프트가 old/new 전문을 주고 'A에서 B로 교체'로
    서술하게 함. diff_parts 가 비어 있어도 '변경 없음'과 구분하기 위한 표지."""

    no_change: bool = False
    """같은 조문의 다른 항이 바뀌어 조문 전체가 개정으로 잡혔지만, 이 항은
    실제로 안 바뀐 경우(normalize(old)==normalize(new)). 요약에서 '변경 없음'
    으로 처리함 — 이걸 표시 안 하면 diff 빈 목록을 '통째 교체'로 오해함."""


@dataclass(frozen=True)
class PositionMapping:
    """구(舊) 위치 ↔ 신(新) 위치 대응 하나.

    relation:
        신설  — 구법에 없던 것이 새로 생김
        삭제  — 신법에서 없어짐
        개정  — 같은 자리에서 내용이 바뀜
        이동  — 내용은 그대로인데 번호만 밀림
        이동후개정 — 번호도 밀리고 내용도 바뀜
        동일  — 자리도 내용도 그대로

    basis:
        완전일치 — 문장이 글자까지 같아 계산으로 확정. 표본과 무관하게 참.
        LLM판정  — 애매해서 에이전트가 판단. 틀릴 수 있음.
        라벨    — 밀림이 불가능한 조문이라 계약의 change_type 을 그대로 사용.
    """

    old_position: str | None
    new_position: str | None
    relation: str
    basis: str
    note: str = ""


@dataclass(frozen=True)
class ArticleMapping:
    """조문 하나에 대한 대응 관계 전체."""

    article_label: str
    mappings: list[PositionMapping] = field(default_factory=list)

    needs_review: bool = False
    """LLM 이 판정했고 확신이 낮은 경우. 보고서에 원문 링크를 함께 냄."""

    error: str | None = None

    @property
    def shifted(self) -> list[PositionMapping]:
        """번호가 밀린 것만. 메일에 '①이 ②로 이동'이라고 쓸 대상."""
        return [m for m in self.mappings if m.relation in ("이동", "이동후개정")]

    def describe(self) -> str:
        """사람이 읽을 수 있는 한 줄 — 로그·검토용."""
        parts = []
        for m in self.mappings:
            if m.relation == "신설":
                parts.append(f"신{m.new_position} 신설")
            elif m.relation == "삭제":
                parts.append(f"구{m.old_position} 삭제")
            elif m.relation in ("이동", "이동후개정"):
                parts.append(f"구{m.old_position}→신{m.new_position} {m.relation}")
            elif m.relation == "개정":
                parts.append(f"{m.old_position} 개정")
        return f"{self.article_label}: " + ", ".join(parts)


@dataclass(frozen=True)
class ArticleSummary:
    """1단계 산출물 — 조문 하나에 대한 요약."""

    unit: ArticleUnit
    summary: str
    caveats: list[str] = field(default_factory=list)
    """match_status 에서 규칙으로 뽑은 신뢰도 경고. LLM 미개입.
    2단계로 그대로 전달해 종합 요약에서도 단정하지 않게 함."""

    error: str | None = None
    """호출 실패 시 사유. 실패를 조용히 빼지 않는다는 계약 원칙과 같음."""


@dataclass(frozen=True)
class VerifierIssue:
    """감수 에이전트가 찾은 문제 하나.

    severity:
        high — 요약이 사실과 다름(환각, 이동/신설 뒤바뀜, 번호 오류)
        low  — 표현이 아쉬움(밋밋한 headline, 핵심 누락)
    """

    severity: str
    where: str
    problem: str


@dataclass(frozen=True)
class LawSummary:
    """2단계 산출물 — 법령 1건에 대한 종합 요약. 최종 출력물."""

    law_id: str
    law_name: str
    law_type: str
    enforce_date: str
    revision_type: str
    source_url: str

    new_serial_no: str
    """이 요약이 어느 개정분에 대한 것인지. change_log/article_diff 의
    같은 이름 컬럼과 같은 값임.

    요약을 DB 에 적재할 때(sinks.DbSink) (law_id, new_serial_no) 가
      키가 됨. law_id 만으로는 부족함 — 같은 법이 여러 번 개정되면
      요약도 여러 건이고, 그것들은 서로 다른 사실을 말함."""

    headline: str
    """한 줄 요약 — 목록 화면용."""

    body: str
    """본문 요약 — LLM 이 쓴 취지 개요(overview) + 코드가 붙인 조문별 목록."""

    overview: str = ""
    """LLM 이 쓴 개정 취지 부분만. 감수 대상이자, 코드가 붙인 구조 목록과
    구분해 두는 용도. body 의 앞부분과 같음."""

    caveats: list[str] = field(default_factory=list)
    """이 요약을 읽는 사람이 알아야 할 신뢰도 경고."""

    article_summaries: list[ArticleSummary] = field(default_factory=list)

    mappings: list[ArticleMapping] = field(default_factory=list)
    """조문별 구↔신 대응. 보고서에 '①이 ②로 이동'을 쓰는 근거이자,
    나중에 판정이 의심스러울 때 추적하는 기록임."""

    verifier_issues: list[VerifierIssue] = field(default_factory=list)
    """Verifier(감수) 에이전트가 body/headline 에서 찾은 문제. high 는 사실 오류라
    담당자가 원문을 봐야 하고, low 는 개선 여지임."""

    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ContractSummary:
    """계약 파일 1개에 대한 요약 묶음.

    single 이든 weekly 든 구조는 같음 — single 은 laws 가 1건일 뿐임.
    """

    source_file: str
    batch_date: str
    laws: list[LawSummary] = field(default_factory=list)

    unresolved: list[dict] = field(default_factory=list)
    """6가드로 위치 확정에 실패한 건. 요약하지 않고 그대로 통과시킴 —
    확정 안 된 것을 문장으로 만들면 그게 환각임."""

    no_comparison: list[dict] = field(default_factory=list)
    """신구법 대비 자체가 불가능한 건(제정 등). 마찬가지로 그대로 통과."""

    def to_dict(self) -> dict:
        return asdict(self)
