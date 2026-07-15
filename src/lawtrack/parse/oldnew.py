"""신구법 <P> 태그 추출 (api/oldnew.py 의 old_texts/new_texts 를 소비).

api 레이어가 넘겨준 raw 텍스트(예: '…「통계법」제27조에 따라 <P>통계청이</P>
공표하는…')에서, 실제로 바뀐 부분만 뽑아내고 변경 유형을 분류한다.

이 모듈이 하지 않는 것:
    - 조/항/호/목 "위치"를 확정하는 것 (그건 locate/locator.py 의 책임 —
      이 모듈은 위치 확정에 쓸 '깨끗한 텍스트'만 준비한다)
    - lawService 전문과의 대조 (역시 locate 의 책임)

실측된 패턴 (전부 검증됨):
    1) 단순 치환    : <P>통계청이</P> → <P>국가데이터처가</P>
    2) 신설         : 구 쪽이 <P><신 설></P>, 신 쪽이 실제 내용 전체
    3) 삭제         : 특정 호가 "<P>1. 삭제</P>" 형태로 표시
    4) 안 바뀜      : "(생 략)" / "(현행과 같음)" — 검색/보고 대상 아님
    5) 대량 치환    : 한 조각 안에 <P> 가 여러 번 (국방데이터·인공지능업무
                      훈령: 위원 명단 조각 하나에 <P> 2회)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)

_P_RE = re.compile(r"<P>(.*?)</P>", re.S)

_SKIP_MARKERS = ("(생 략)", "(생략)", "(현행과 같음)", "(현행과같음)")

#: <P> 안쪽 내용이 이 문구를 포함하면 "이 자리에 원래 없었다(신설)"는 표시.
_NEWLY_CREATED_MARKERS = ("<신 설>", "<신설>", "신 설", "신설")

#: <P> 안쪽 내용이 이 패턴이면 "이 항목이 삭제됐다"는 표시.
_DELETED_RE = re.compile(r"^\s*\S*\s*삭\s*제\s*$")


class ChangeType(str, Enum):
    AMENDED = "개정"
    NEWLY_CREATED = "신설"
    DELETED = "삭제"
    UNCHANGED = "변경없음"
    UNKNOWN = "미상"


def strip_p_tags(text: str) -> str:
    """<P>…</P> 를 벗기고 안쪽 텍스트만 남긴 '깨끗한' 문장.

    locate 단계에서 lawService 전문과 대조할 때 쓴다. 전문 쪽에는애초에
    <P> 태그가 없으므로, 태그가 남아있으면 절대 매칭되지 않는다.
    """
    return _P_RE.sub(lambda m: m.group(1), text or "")


def extract_p_fragments(text: str) -> list[str]:
    """<P>…</P> 안쪽 내용만 순서대로 추출."""
    return [m.group(1) for m in _P_RE.finditer(text or "")]


def is_skippable(text: str) -> bool:
    """(생 략) / (현행과 같음) — 안 바뀐 조각인지."""
    compact = (text or "").replace(" ", "")
    return any(m.replace(" ", "") in compact for m in _SKIP_MARKERS)


def _is_newly_created_marker(fragment: str) -> bool:
    compact = fragment.strip()
    return any(m in compact for m in _NEWLY_CREATED_MARKERS) and len(compact) <= 10


def _is_deleted_marker(fragment: str) -> bool:
    return bool(_DELETED_RE.match(fragment or ""))


@dataclass(frozen=True)
class ArticleChange:
    """조각 하나(구/신 한 쌍)에 대한 변경 분석 결과."""

    index: int
    change_type: ChangeType

    old_raw: str    # <P> 포함 원문 (구)
    new_raw: str    # <P> 포함 원문 (신)
    old_clean: str  # <P> 제거 — locate 검색용 (구)
    new_clean: str  # <P> 제거 — locate 검색용 (신)

    old_fragments: tuple[str, ...] = field(default_factory=tuple)  # <P> 안쪽만 (구)
    new_fragments: tuple[str, ...] = field(default_factory=tuple)  # <P> 안쪽만 (신)

    @property
    def diff_pairs(self) -> list[tuple[str, str]]:
        """구/신 <P> 조각을 위치별로 짝지은 것.

        국방데이터·인공지능업무 훈령 사례처럼 한 조각에 <P> 가 여러 개면
        (위원 명단 나열) 등장 순서로 짝짓는다. 개수가 다르면 짧은 쪽
        기준으로 자르고 남은 쪽은 버리지 않고 단독 항목으로 남긴다.
        """
        n = min(len(self.old_fragments), len(self.new_fragments))
        pairs = list(zip(self.old_fragments[:n], self.new_fragments[:n]))
        if len(self.old_fragments) != len(self.new_fragments):
            log.debug(
                "index=%d <P> 개수 불일치: 구=%d 신=%d (짝 안 맞는 나머지는 diff_pairs에서 제외됨)",
                self.index, len(self.old_fragments), len(self.new_fragments),
            )
        return pairs


def classify(old_text: str, new_text: str) -> ChangeType:
    """구/신 한 쌍의 변경 유형 분류."""
    old_text = old_text or ""
    new_text = new_text or ""

    old_skip = is_skippable(old_text)
    new_skip = is_skippable(new_text)
    if old_skip and new_skip:
        return ChangeType.UNCHANGED

    old_frags = extract_p_fragments(old_text)
    new_frags = extract_p_fragments(new_text)

    if any(_is_newly_created_marker(f) for f in old_frags) and new_frags:
        return ChangeType.NEWLY_CREATED
    if any(_is_deleted_marker(f) for f in new_frags):
        return ChangeType.DELETED
    if not old_frags and not new_frags:
        # <P> 표시가 아예 없는데 텍스트가 다르면 원인 불명 — 조용히 넘기지 않는다.
        if strip_p_tags(old_text) != strip_p_tags(new_text):
            return ChangeType.UNKNOWN
        return ChangeType.UNCHANGED
    return ChangeType.AMENDED


def build_change(index: int, old_text: str, new_text: str) -> ArticleChange:
    """조각 하나를 분석해 ArticleChange 로 만든다."""
    return ArticleChange(
        index=index,
        change_type=classify(old_text, new_text),
        old_raw=old_text or "",
        new_raw=new_text or "",
        old_clean=strip_p_tags(old_text or ""),
        new_clean=strip_p_tags(new_text or ""),
        old_fragments=tuple(extract_p_fragments(old_text or "")),
        new_fragments=tuple(extract_p_fragments(new_text or "")),
    )


def extract_changes(old_texts: list[str], new_texts: list[str]) -> list[ArticleChange]:
    """api/oldnew.py 의 old_texts/new_texts (같은 인덱스=같은 조문)를
    분석해 실제로 검토할 가치가 있는 변경 목록만 반환한다.

    UNCHANGED 는 결과에서 제외한다 — (생 략)/(현행과 같음) 은 애초에
    바뀐 게 아니므로 이후 파이프라인(locate, DB 적재)에서 다룰 필요가
    없다.
    """
    if len(old_texts) != len(new_texts):
        log.warning(
            "구조문/신조문 개수 불일치: 구=%d 신=%d — 짧은 쪽 기준으로 짝짓고 "
            "나머지는 버려짐 (원인 파악 필요할 수 있음)",
            len(old_texts), len(new_texts),
        )

    n = min(len(old_texts), len(new_texts))
    changes = []
    for i in range(n):
        change = build_change(i, old_texts[i], new_texts[i])
        if change.change_type is not ChangeType.UNCHANGED:
            changes.append(change)
    return changes