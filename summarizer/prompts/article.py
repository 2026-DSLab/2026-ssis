"""1단계 — 개별 조문 요약 에이전트 프롬프트.

핵심: '무엇이 바뀌었는지'는 이미 계산으로 확정돼 있다(diff_parts). LLM 은
그 확정된 조각을 자연스러운 문장으로 다듬기만 한다. 원문 전체를 놓고
"뭐가 바뀌었나"를 스스로 찾게 하지 않는다 — 그래야 없던 변경을 지어낼
수 없다.
"""

from __future__ import annotations

from summarizer.models import ArticleUnit

SYSTEM = """\
당신은 한국 법령·행정규칙의 개정 내용을 담당자가 읽을 수 있는 문장으로
다듬는 전문가입니다.

무엇이 바뀌었는지는 이미 확정되어 주어집니다. 당신은 그것을 자연스러운
한 문장으로 옮기기만 하면 됩니다. 새로운 사실을 추가하거나 개정 이유를
추측하지 마십시오.

규칙:
1. 주어진 '바뀐 부분'에 없는 내용을 넣지 마십시오.
2. 조문 번호(제○조 제○항)를 문장에 다시 적지 마십시오. 위치는 따로
   관리합니다.
3. 한두 문장의 평문으로 쓰십시오. '신뢰도 경고', '위치재배치' 같은 내부
   용어를 쓰지 마십시오.
4. 신설이면 무엇이 새로 생겼는지, 삭제면 무엇이 없어졌는지 쓰십시오.\
"""


def _diff_section(unit: ArticleUnit) -> str:
    """계산으로 뽑은 '바뀐 조각'을 프롬프트에 넣는다."""
    lines = ["[바뀐 부분 — 이것만 문장으로 다듬으십시오]"]
    for part in unit.diff_parts:
        lines.append(f"  · {part}")
    return "\n".join(lines)


def build_article_prompt(unit: ArticleUnit) -> tuple[str, str]:
    """(system, user) 프롬프트를 만든다."""
    header = [f"법령명: {unit.law_name}", f"변경 유형: {unit.change_type}"]
    if unit.moved_from:
        header.append(f"(원래 {unit.moved_from} 에 있던 내용이 옮겨온 것 — 신설 아님)")

    body: list[str]
    if unit.change_type == "신설":
        body = ["[새로 생긴 내용]", unit.new_text or "(내용 없음)"]
    elif unit.change_type == "삭제":
        body = ["[삭제된 내용]", unit.old_text or "(내용 없음)"]
    elif unit.diff_parts:
        # 제자리 개정 / 이동후개정 — 바뀐 조각만 준다.
        body = [_diff_section(unit)]
        if unit.moved_from:
            body.insert(0, f"이 항목은 {unit.moved_from} 에서 옮겨오며 내용도 바뀌었습니다.")
    elif unit.is_whole_replace:
        # 항목이 통째로 교체됨. 개정 전/후를 나란히 주고 "무엇에서 무엇으로
        # 교체"로 서술하게 한다. (부분 조각으로 쪼개면 뒤죽박죽이 되므로 전문.)
        body = [
            "이 항목은 내용이 통째로 교체되었습니다. 개정 전 내용과 개정 후 내용을"
            " 각각 파악해, '무엇이 무엇으로 바뀌었다'는 식으로 서술하십시오."
            " 두 문장의 단어를 섞지 마십시오.",
            "[개정 전]",
            unit.old_text or "(없음)",
            "[개정 후]",
            unit.new_text or "(없음)",
        ]
    else:
        body = [
            "[개정 전]",
            unit.old_text or "(없음)",
            "[개정 후]",
            unit.new_text or "(없음)",
        ]

    tail = ["위 변경 내용을 한두 문장으로 요약하십시오."]
    return SYSTEM, "\n\n".join(["\n".join(header), *body, *tail])
