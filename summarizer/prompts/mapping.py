"""매핑 에이전트 프롬프트 — 계산으로 못 푼 것만 대조.

여기 오는 것은 이미 좁혀진 문제다. 번호가 양쪽에 멀쩡히 있는 항목은
계산이 제자리 개정으로 확정했고, 문장이 그대로 보존된 이동도 확정했다.
남은 것은 "사라진 old" 와 "출처 없는 new" — 이 둘의 짝을 찾는 일이다.

실측(전자정부법 제56조의2): 구②(위임규정)가 신⑤ 로 내려가며 "제1항에
따른"→"제1항부터 제4항까지에 따른" 으로 확장됐다. 문장이 바뀌어 부분
문자열로는 못 잡지만, 내용을 읽으면 같은 취지임이 보인다.
"""

from __future__ import annotations

from typing import Sequence

from lawtrack.contract.schema import ArticleDiffItem

from summarizer.matching import position_label

SYSTEM = """\
당신은 한국 법령 개정에서 조문 구조 변화를 판정하는 전문가입니다.

한 조문 안에서, 개정으로 '사라진 항목'과 '새로 나타난 항목' 목록이
주어집니다. 사라진 항목의 내용이 새로 나타난 항목 중 하나로 옮겨간
것인지(이동), 아니면 정말 없어진 것인지(삭제) 판정하십시오.

판정 기준:
1. 사라진 항목의 내용이 새 항목 중 하나에 같은 취지로 담겨 있으면 '이동'
   입니다. 문장이 다듬어졌거나 범위가 확장됐어도(예: "제1항에 따른" →
   "제1항부터 제4항까지에 따른") 핵심 내용이 같으면 이동입니다.
2. 옮겨가면서 실질적으로 바뀌었으면 '이동후개정'입니다.
3. 새 항목 중 사라진 어느 것과도 대응하지 않는 것은 '신설'입니다.
4. 사라진 항목 중 어디에도 옮겨가지 않은 것은 '삭제'입니다.

주의:
- 번호가 아니라 내용을 보고 판단하십시오.
- 억지로 짝짓지 마십시오. 확신이 없으면 confidence 를 low 로 하고,
  대응이 불분명한 것은 신설/삭제로 두십시오. 틀린 확신보다 낫습니다.
- 사라진 모든 항목과 새로 나타난 모든 항목이 결과에 한 번씩 나와야 합니다.\
"""


MAPPING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "old_position": {
                        "type": ["string", "null"],
                        "description": "사라진 항목 위치. 예: '②'. 신설이면 null",
                    },
                    "new_position": {
                        "type": ["string", "null"],
                        "description": "새 항목 위치. 삭제면 null",
                    },
                    "relation": {
                        "type": "string",
                        "enum": ["이동", "이동후개정", "신설", "삭제"],
                    },
                    "note": {"type": "string", "description": "판단 근거 한 문장"},
                },
                "required": ["old_position", "new_position", "relation", "note"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": ["mappings", "confidence"],
    "additionalProperties": False,
}


def build_mapping_prompt(
    law_name: str,
    article_label: str,
    pool_old: Sequence[ArticleDiffItem],
    pool_new: Sequence[ArticleDiffItem],
) -> tuple[str, str]:
    """(system, user) 프롬프트를 만든다."""
    lines = [f"법령명: {law_name}", f"조문: {article_label}", "", "[사라진 항목 (개정 전)]"]
    if pool_old:
        for it in pool_old:
            lines.append(f"  {position_label(it)}  {it.old_text}")
    else:
        lines.append("  (없음)")

    lines += ["", "[새로 나타난 항목 (개정 후)]"]
    if pool_new:
        for it in pool_new:
            lines.append(f"  {position_label(it)}  {it.new_text}")
    else:
        lines.append("  (없음)")

    lines += [
        "",
        "사라진 항목이 새 항목 중 하나로 옮겨간 것인지, 아니면 삭제/신설인지 판정하십시오.",
    ]
    return SYSTEM, "\n".join(lines)
