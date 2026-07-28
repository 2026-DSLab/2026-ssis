"""결정론적 본문 조립 — 구조 사실은 코드가 쓴다.

★ 왜 필요한가 (2026-07-25 실측, 범죄피해자 제47조):
    LawAgent(LLM)에게 body 를 자유롭게 쓰게 했더니 이동 항목을 "신설"로
    뭉뚱그렸다. 매핑은 "구②→④ 이동"을 정확히 잡았는데도, LLM 이 산문으로
    풀어 쓰는 과정에서 "제47조가 신설되었습니다"라고 틀리게 서술했다.

    구조 사실(어느 조가 신설/개정/이동/삭제인가)을 LLM 판단에 맡기면
    모델 크기와 무관하게 확률적으로 틀린다. 그래서 그 부분은 코드가 쓴다 —
    매핑(12개 그룹 검증 완료)과 조문 요약을 그대로 박으므로 절대 안 틀린다.
    LLM 은 '개정 취지 개요'만 쓴다(그건 revision_reason 을 다듬는 일이라
    구조를 헷갈릴 여지가 없다).
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Sequence

from summarizer.models import ArticleSummary

_ARTICLE = re.compile(r"^제\d+조(?:의\d+)?")

_TAG = {
    "신설": "신설",
    "개정": "개정",
    "삭제": "삭제",
    "이동": "이동",
    "이동후개정": "이동·개정",
    "미상": "변경",
}


def _article_of(location_label: str) -> str:
    m = _ARTICLE.match(location_label)
    return m.group(0) if m else location_label


def _position_of(location_label: str) -> str:
    """조문 라벨을 뗀 위치 부분. 예: '제47조①' → '①'."""
    art = _article_of(location_label)
    return location_label[len(art):] if location_label.startswith(art) else ""


def build_change_section(summaries: Sequence[ArticleSummary]) -> str:
    """조문별 변경 내용을 결정론적으로 조립한다.

    각 항목의 change_type 은 apply_mappings 가 교정한 값이라 정확하다.
    이동 항목은 조문 요약에 이미 "○에서 ○로 번호만 이동" 문장이 들어 있다.
    """
    if not summaries:
        return ""

    by_art: "OrderedDict[str, list[ArticleSummary]]" = OrderedDict()
    for s in summaries:
        by_art.setdefault(_article_of(s.unit.location_label), []).append(s)

    lines = ["○ 조문별 변경 내용"]
    for art, items in by_art.items():
        if len(items) == 1:
            s = items[0]
            lines.append(f"- {art} ({_tag(s)}): {_text(s)}")
            continue

        # 한 조문이 통째로 신설된 경우(모든 하위 항목이 신설) 개별 호·목을
        # 다 펼치지 않는다 — 수십 줄이 되어 담당자가 읽기 어렵다. "조문 전체
        # 신설"로 묶고, 대표 내용(첫 항목) 한 줄만 보인다. 세부는 원문 참조.
        if all(s.unit.change_type == "신설" for s in items):
            head = next((_text(s) for s in items if _text(s) and "실패" not in _text(s)), "")
            lines.append(f"- {art} (신설, {len(items)}개 항·호·목): {head}")
            continue

        lines.append(f"- {art}")
        for s in items:
            pos = _position_of(s.unit.location_label) or "전체"
            lines.append(f"    · {pos} ({_tag(s)}): {_text(s)}")
    return "\n".join(lines)


def _tag(s: ArticleSummary) -> str:
    return _TAG.get(s.unit.change_type, s.unit.change_type)


def _text(s: ArticleSummary) -> str:
    if s.error:
        return "[요약 생성 실패 — 원문 확인 필요]"
    return s.summary.strip() or "[내용 없음]"
