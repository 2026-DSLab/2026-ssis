"""2단계 — 법령 단위 종합 요약 에이전트 프롬프트."""

from __future__ import annotations

from typing import Sequence

from lawtrack.contract.schema import LawChange

from summarizer.models import ArticleMapping, ArticleSummary

SYSTEM = """\
당신은 한국 법령·행정규칙 개정 보고서의 '머리말'을 쓰는 전문가입니다.

★ 중요: 어느 조·항이 신설/개정/이동/삭제되었는지 '조문별 목록'은 시스템이
   따로 정확하게 붙입니다. 당신은 그 목록을 쓰지 않습니다. 당신은 (1) 제목과
   (2) 이 개정이 전체적으로 무엇을 위한 것인지 2~3문장 개요만 씁니다.

주어지는 것:
  - 법제처 공식 개정이유 (있는 경우)
  - 바뀐 조문들의 개별 요약 (참고용 — 취지를 파악하는 데만 쓰십시오)

당신이 쓸 것:

  headline — 이 개정에서 '무엇이 바뀌었는지'를 담은 한 문장. 목록 화면과
             메일 제목에 그대로 노출되므로, 이것만 봐도 핵심을 알 수 있어야
             합니다.
               · 법령명을 넣지 마십시오(이미 따로 표시됩니다).
               · "일부개정", "개정 사항", "정리" 같은 내용 없는 말로 끝내지
                 마십시오. 어느 법에나 붙어 무의미합니다.
               · 바뀐 핵심을 구체적으로 쓰십시오.
             나쁜 예: "○○법 일부개정 사항 정리", "일부 조문 개정"
             좋은 예: "과징금 산정 시 매출액 기준 변경 및 감경 예외 신설",
                     "범죄피해자 인권주간 신설 및 구조금 분할지급 도입"

  overview — 이 개정이 전체적으로 무엇을 위한 것인지 2~3문장. 담당자가
             "우리 업무에 영향이 있나"를 판단할 수 있게 취지 중심으로
             쓰십시오.
               · 개별 조·항 번호를 나열하지 마십시오(아래 목록이 담당).
               · "제○조가 신설/개정/이동" 같은 구조 서술을 하지 마십시오.
                 그건 시스템이 정확히 붙이므로, 당신이 쓰면 틀릴 수 있고
                 중복됩니다.
               · 개정이유가 비어 있으면 취지를 추측하지 말고, 바뀐 내용의
                 공통 주제만 한 문장으로 쓰십시오.
               · 한글로만 쓰십시오. 한자를 쓰지 마십시오("구체적"을 "具체적"
                 으로 쓰는 오류 주의). 단, 법조문 원래의 병기("과(科)")는 예외.\
"""


# 구조 서술(신설/이동 등)은 코드가 담당하므로, LLM 에게는 제목과 개요만
# 받는다. body 를 통째로 맡기지 않는 것이 이 설계의 핵심이다.
LAW_SUMMARY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "이 개정이 무엇인지 한 문장. 법령명·'일부개정' 같은 빈 말 금지.",
        },
        "overview": {
            "type": "string",
            "description": "개정 취지 2~3문장. 조·항 번호나 신설/이동 서술은 넣지 않는다.",
        },
    },
    "required": ["headline", "overview"],
    "additionalProperties": False,
}


def _revision_reason_section(law: LawChange) -> str:
    if not law.revision_reason.strip():
        return "[공식 개정이유] (없음 — 취지를 추측하지 말 것)"
    return f"[공식 개정이유]\n{law.revision_reason}"


def _article_section(summaries: Sequence[ArticleSummary]) -> str:
    if not summaries:
        return "[조문별 변경] (조문 단위 변경 없음)"

    lines = ["[조문별 변경]"]
    for i, item in enumerate(summaries, start=1):
        unit = item.unit
        lines.append(f"\n{i}. {unit.location_label} ({unit.change_type})")
        if item.error:
            lines.append(f"   ※ 요약 실패: {item.error}")
            continue
        lines.append(f"   {item.summary}")
        for caveat in item.caveats:
            lines.append(f"   ※ 신뢰도 경고: {caveat}")
    return "\n".join(lines)


def _unchanged_section(law: LawChange) -> str:
    if not law.unchanged_clauses:
        return ""
    lines = ["[이번에 바뀌지 않은 항/호 — 변경된 것처럼 쓰지 말 것]"]
    for article_label, labels in law.unchanged_clauses.items():
        lines.append(f"  {article_label}: {', '.join(labels)}")
    return "\n".join(lines)


def _mapping_section(mappings: Sequence[ArticleMapping]) -> str:
    """구↔신 대응 판정 결과.

    계약 JSON 의 change_type 은 순서 기준 짝짓기라 밀림을 반영하지 못한다.
    여기 있는 값이 그것을 바로잡은 결과이므로 프롬프트에서 우선순위를
    명시해야 한다.
    """
    interesting = [m for m in mappings if m.shifted or m.needs_review]
    if not interesting:
        return ""

    lines = ["[조문 구조 변경 — 이것이 정답입니다. 조문별 요약보다 우선]"]
    for m in interesting:
        lines.append(f"  {m.describe()}")
        if m.needs_review:
            lines.append("    ※ 판정 확신 낮음 — 단정적으로 쓰지 말 것")
    return "\n".join(lines)


def build_law_prompt(
    law: LawChange,
    summaries: Sequence[ArticleSummary],
    mappings: Sequence[ArticleMapping] = (),
) -> tuple[str, str]:
    """(system, user) 프롬프트를 만든다."""
    sections = [
        "\n".join(
            [
                f"법령명: {law.law_name}",
                f"종류: {law.law_type}",
                f"개정 유형: {law.revision_type or '(미상)'}",
                f"시행일: {law.enforce_date or '(미상)'}",
            ]
        ),
        _revision_reason_section(law),
    ]

    # 조문별 요약은 '취지 파악용 참고 자료'로만 넣는다. 조문 목록 자체는
    # 코드(render.build_change_section)가 붙이므로 LLM 은 이걸 베끼지 않는다.
    sections.append(_article_section(summaries))

    sections.append(
        "위 자료를 참고해 headline(제목 한 줄)과 overview(개정 취지 2~3문장)만"
        " 쓰십시오. 조·항 번호 나열이나 신설/이동 서술은 하지 마십시오 —"
        " 그건 시스템이 따로 붙입니다."
    )

    return SYSTEM, "\n\n".join(sections)
