"""사전 선별 — LLM 투입 전 결정론적 분류.

실측 근거(article_diff 704건, 개정 조문 기준):
    유사도 >0.95 …… 369건 (52%)   ← 대부분 정부조직 개편에 따른 기관명 정비
    0.8~0.95 ……… 108건 (15%)
    <0.8 ………… 227건 (32%)   ← 실질 변경

    타법개정 294건 중 84% 가 >0.95 구간이고, 실제 치환 내용은
        '통계청장이'  -> '국가데이터처장이'
        '기획재정부'  -> '기획예산처'
        '여성가족부'  -> '성평등가족부'
    같은 기관명 일괄 정비였다.

이 절반을 LLM 에 보내지 않으면 비용과 보고서 노이즈가 동시에 준다.

--------------------------------------------------------------------------
[안전 원칙 — 이 모듈에서 가장 중요한 것]

    오분류의 두 방향은 비용이 전혀 다르다.

      실질 변경 → FORMAL 로 오분류  : 보고서에서 누락된다. 치명적.
      형식 정비 → SUBSTANTIVE 로 오분류 : LLM 호출 한 번 더. 사소하다.

    따라서 이 계층은 확신이 있을 때만 FORMAL 을 준다.
    조금이라도 애매하면 BORDERLINE 으로 넘겨 LLM 이 판단하게 한다.
    "거르는 것"이 목적이 아니라 "확실한 것만 빼는 것"이 목적이다.
--------------------------------------------------------------------------

★ 출처: seongbeen2 브랜치(lawtrack.mas.triage)에서 이식(2026-07-30).
  ArticleAgent.run()(summarizer/agents.py)이 LLM을 부르기 전에 이 판정을
  거친다 — move_is_identical/no_change와 같은 자리에 있는, 세 번째
  규칙기반 스킵 경로다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from summarizer.textdiff import changed_words, similarity

__all__ = ["ChangeClass", "TriageResult", "triage"]


class ChangeClass(str, Enum):
    NO_CHANGE = "변경없음"
    """개정 전후 본문이 동일한 행. 상류 파이프라인의 산물이며 보고서에서 제외한다.

    실측: 1002건 중 28건이 old_text == new_text 였고 26건은 match_status='성공'
    이었다. 대부분 행정규칙(조달청 내자구매업무 처리규정 10, 계약예규 15).
    신구조문대비표에서 변경되지 않은 항목까지 행으로 뜬 것으로 보인다.
    상류를 고치기 전까지 여기서 막는다."""

    FORMAL = "형식정비"
    """기관명·인용법령명 정비 등 실무 영향이 없는 변경. 부록으로 보낸다."""

    SUBSTANTIVE = "실질변경"
    """내용이 바뀐 변경. 요약 대상."""

    BORDERLINE = "판단필요"
    """결정론적으로 확신할 수 없음. LLM 분류 에이전트로 보낸다."""


# 기관·직위명 판정.
#
# [실패 사례 — 테스트가 잡아낸 것]
#     접미사에 '원'을 넣었더니 '100만원으로' -> '200만원으로' 가
#     기관명 정비로 분류됐다. 금액 변경이 보고서에서 누락될 뻔했다.
#     '국'(전국·외국), '과'(결과·경과), '실'(사실·현실) 도 같은 문제다.
#
#     => 한 글자 접미사 중 모호한 것은 전부 뺀다.
#        '원'이 필요한 기관은 진흥원·연구원처럼 통째로 적는다.
#        접미사 앞에 최소 2음절을 요구해 '일부를'·'전부를' 같은 일반어를 막는다.
#        숫자가 섞인 어절은 무조건 제외한다 — 수치 변경은 실질 변경이다.

_JOSA = r"(?:이|가|은|는|을|를|의|에|에게|와|과|으로|로|께|에서|이라|라|도|만)?"

_ORG_TITLE = r"(?:장관|차관|청장|처장|원장|국장|실장|과장|위원장|본부장|단장|총장)"
"""직위명. 그 자체로 모호하지 않다."""

_ORG_BODY = r"(?:위원회|공단|공사|재단|진흥원|연구원|부|처|청)"
"""기관명 어미. 앞에 2음절 이상을 요구해야 안전하다."""

_ORG_SUFFIX = re.compile(rf"^[가-힣]{{2,}}(?:{_ORG_TITLE}|{_ORG_BODY}){_JOSA}$")

_ORG_TITLE_ONLY = re.compile(rf"^[가-힣]{{2,}}{_ORG_TITLE}{_JOSA}$")

_HAS_DIGIT = re.compile(r"\d")


def _org_category(word: str) -> str | None:
    """기관명 어절의 범주. 'title'(기관장 직위) | 'body'(기관) | None."""
    if _HAS_DIGIT.search(word):
        return None
    if _ORG_TITLE_ONLY.match(word):
        return "title"
    if _ORG_SUFFIX.match(word):
        return "body"
    return None

# 「」 안의 인용 법령명
_CITED_LAW = re.compile(r"[「『][^」』]{2,60}[」』]")

# 이 임계값들은 위 실측 분포에서 나왔다. 보수적으로 잡혀 있다.
_FORMAL_MAX_WORDS = 4
"""변경 어절이 이보다 많으면 단순 명칭 정비로 보기 어렵다.

FORMAL 판정의 실질적인 근거는 유사도가 아니라 '변경 어절이 전부
기관·직위명인가' 이다. 유사도를 함께 요구했더니 짧은 조문에서
어절 하나만 바뀌어도(예: 5어절 중 1개 → 유사도 0.80) 문턱에 걸려
LLM 으로 밀려났다. 기관명 판정이 이미 충분히 좁으므로 유사도는
아래 _FORMAL_MIN_SIM 을 하한선으로만 쓴다."""

_FORMAL_MIN_SIM = 0.50
"""SequenceMatcher 가 이상 정렬을 낸 경우를 막는 하한선.
정상적인 명칭 정비라면 이 값을 밑돌 수 없다."""

_SUBSTANTIVE_MAX_SIM = 0.80
"""이 아래면 결정론적으로 실질 변경 확정 — 단, 기관명 정비는 예외로
먼저 검사한다(짧은 조문은 명칭 하나만 바뀌어도 이 값을 밑돈다)."""


@dataclass(frozen=True)
class TriageResult:
    change_class: ChangeClass
    similarity: float
    deleted_words: list[str] = field(default_factory=list)
    inserted_words: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def needs_llm(self) -> bool:
        """LLM 을 태워야 하는가."""
        return self.change_class in (ChangeClass.SUBSTANTIVE, ChangeClass.BORDERLINE)


def _is_org_rename(dels: list[str], inss: list[str]) -> bool:
    """기관·직위명이 '이름만' 바뀌었는가.

    [실패 사례 — 실데이터에서 잡힌 것]
        '심의위원회가' -> '보건복지부장관이'
        양쪽 다 기관명 패턴에 맞아 형식 정비로 분류됐다. 그러나 이것은
        개편이 아니라 권한 이관이다 — 심의 권한이 위원회에서 장관으로
        넘어간 실질 변경이며, 보고서에서 누락되면 안 된다.

        => 기관(body)과 기관장 직위(title)는 서로 다른 범주로 보고,
           범주가 바뀌면 명칭 정비로 인정하지 않는다.
           '통계청장'->'국가데이터처장'(title→title)은 통과하고
           '심의위원회'->'보건복지부장관'(body→title)은 걸린다.
    """
    if not dels or not inss or len(dels) != len(inss):
        return False  # 1:1 대응이 아니면 명칭 치환으로 볼 수 없다

    for old_w, new_w in zip(dels, inss):
        old_cat, new_cat = _org_category(old_w), _org_category(new_w)
        if old_cat is None or new_cat is None or old_cat != new_cat:
            return False
    return True


# 조문 인용 표기(제12조, 제2항, 제3호, 가목…). 숫자가 들어 있지만
# 수치 변경이 아니라 인용 정비다.
_CITATION_TOKEN = re.compile(r"^[「『]?제?\d+[조항호목절관장편]")


def _is_numeric_change(dels: list[str], inss: list[str]) -> bool:
    """기한·금액·비율·인원 등 수치가 바뀌었는가.

    [이 검사가 필요한 이유 — 테스트가 잡은 위험]
        '100만원으로' -> '200만원으로' 는 어절 유사도가 0.83 이라
        BORDERLINE(판단 유보)으로 빠진다. 위임 규칙에서는 LLM 이
        형식정비라고 답하면 그대로 형식정비가 되어 금액 변경이
        보고서에서 누락된다.

        수치 변경은 실무 영향이 사실상 확실하므로 코드가 확정 판단해
        위임 구간에서 빼낸다. 단, '제47조' -> '제47조제2항' 같은
        조문 인용은 숫자가 있어도 수치 변경이 아니다.
    """
    words = [w for w in dels + inss if _HAS_DIGIT.search(w)]
    if not words:
        return False
    if all(_CITATION_TOKEN.match(w) for w in words):
        return False  # 조문 인용 정비
    # 양쪽에서 뽑은 숫자가 실제로 달라야 수치 변경이다
    nums = lambda ws: [n for w in ws for n in re.findall(r"\d+", w)]
    return nums(dels) != nums(inss)


def _is_citation_change(old: str, new: str, dels: list[str], inss: list[str]) -> bool:
    """변경분이 인용 법령명 안에서만 발생했는가(타법개정의 전형)."""
    if not (dels or inss):
        return False
    old_cites = set(_CITED_LAW.findall(old))
    new_cites = set(_CITED_LAW.findall(new))
    if old_cites == new_cites:
        return False
    changed_cite_text = "".join(old_cites ^ new_cites)
    return all(w.strip("「」『』,.") in changed_cite_text for w in dels + inss)


def triage(old_text: str, new_text: str, *, change_type: str = "") -> TriageResult:
    """조문 하나를 분류한다.

    change_type 은 article_diff.change_type('개정'/'신설'/'삭제').
    신설·삭제는 내용 자체가 생기거나 사라진 것이므로 형식 정비일 수 없다.
    """
    old = (old_text or "").strip()
    new = (new_text or "").strip()

    if change_type in ("신설", "삭제"):
        return TriageResult(
            ChangeClass.SUBSTANTIVE, 0.0,
            reason=f"{change_type}은 형식 정비가 될 수 없음",
        )

    if not old or not new:
        return TriageResult(
            ChangeClass.SUBSTANTIVE, 0.0,
            reason="한쪽 본문이 비어 있어 대조 불가 — 보수적으로 실질 변경 처리",
        )

    sim = similarity(old, new)
    dels, inss = changed_words(old, new)

    if not dels and not inss:
        return TriageResult(ChangeClass.NO_CHANGE, sim,
                            reason="개정 전후 본문 동일 — 상류 파이프라인 산물")

    # 명칭 정비 검사가 먼저다. 짧은 조문은 명칭 하나만 바뀌어도
    # 유사도가 실질 변경 구간까지 떨어지기 때문이다.
    if sim >= _FORMAL_MIN_SIM and len(dels) + len(inss) <= _FORMAL_MAX_WORDS:
        if _is_org_rename(dels, inss):
            return TriageResult(ChangeClass.FORMAL, sim, dels, inss,
                                reason="변경 어절이 모두 기관·직위명 — 조직 개편 정비")
        if _is_citation_change(old, new, dels, inss):
            return TriageResult(ChangeClass.FORMAL, sim, dels, inss,
                                reason="인용 법령명만 변경 — 타법개정 정비")

    # 수치 변경은 유사도와 무관하게 확정 판단한다. BORDERLINE 으로 흘려보내면
    # LLM 이 형식정비라 답했을 때 금액·기한 변경이 그대로 누락된다.
    if _is_numeric_change(dels, inss):
        return TriageResult(ChangeClass.SUBSTANTIVE, sim, dels, inss,
                            reason="수치(기한·금액·비율 등) 변경 — 실무 영향 확실")

    if sim < _SUBSTANTIVE_MAX_SIM:
        return TriageResult(ChangeClass.SUBSTANTIVE, sim, dels, inss,
                            reason=f"유사도 {sim:.2f} — 실질 변경")

    return TriageResult(ChangeClass.BORDERLINE, sim, dels, inss,
                        reason=f"유사도 {sim:.2f} — 결정론적 확신 없음, LLM 판단 필요")
