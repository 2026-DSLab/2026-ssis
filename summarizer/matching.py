"""조문 안에서 구(舊) ↔ 신(新) 대응 판정 — 결정론적 부분.

★ 왜 필요한가:
    법제처 신구조문대비표는 개정 전/후를 '순서'로만 짝짓는다. 항이 중간에
    신설되면 뒤 항들이 밀리는데, 대비표는 그걸 모른 채 자리끼리 나란히
    놓는다. 그래서 계약 JSON 의 change_type/짝짓기가 실제와 어긋난다.

    실측(범죄피해자 보호법 제47조):
        실제:  구① → 신②,  구② → 신④,  신①③ 이 진짜 신설
        JSON:  신④ = "신설"(밀려온 기존 조항인데), 신② = "개정"(밀림인데)

    이 라벨을 그대로 요약하면 "④항이 신설되었습니다"라는 잘못된 메일이
    나간다. 담당자는 없던 처벌 조항이 생긴 줄 안다.

★ 판정 원칙 (번호를 기준으로 삼는다):
    번호가 양쪽에 다 있고 짝을 믿을 수 있으면 → 제자리 개정. 이동 아님.
    번호가 한쪽에만 있거나 짝이 의심스러우면 → '풀'에 모아 내용으로 대조:
        사라진 내용이 다른 번호의 새 항목에 그대로 있으면 → 이동
        같은 번호끼리 남으면 → 제자리 개정(의심이 틀렸던 것)
        끝내 안 맞으면 → 삭제 / 신설

    임계값(유사도 0.7 등)을 쓰지 않는다. 임계값은 표본에서 뽑은 숫자라
    새 법령에서 조용히 틀린다. "부분문자열로 그대로 보존"은 표본과 무관하게
    참이다. 계산으로 확정 못 하는 소수(문장이 확장되며 옮겨간 경우)만
    MappingAgent(LLM)로 넘긴다.

    실측(2026-07-22): 12개 조문 그룹 중 11개가 이 계산만으로 정답.
    나머지 1개(전자정부법 제56조의2, "제1항에 따른"→"제1항부터 제4항까지에
    따른" 으로 확장되며 이동)만 부분문자열로 못 잡아 LLM 이 필요하다.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict

from lawtrack.contract.schema import ArticleDiffItem, LawChange

from summarizer.models import PositionMapping

# 문장 앞의 항/호/목 번호. 내용 비교 전에 떼어낸다 — 번호가 밀리면 이
# 부분만 달라지므로, 붙여둔 채 비교하면 같은 내용을 다른 것으로 본다.
_LEADING_LABEL = re.compile(r"^\s*(?:[0-9]+\.|[가-힣]\.|[①-⑳])\s*")


def normalize(text: str) -> str:
    """내용 비교용 정규화 — 앞 번호 제거 + 공백 전부 제거."""
    return re.sub(r"\s+", "", _LEADING_LABEL.sub("", text or ""))


def position_label(item: ArticleDiffItem) -> str:
    """조문 안에서의 위치. 예: '①7.'"""
    return "".join(p for p in (item.clause_no, item.item_label, item.subitem_label) if p)


def depth(item: ArticleDiffItem) -> int:
    """항(1) / 호(2) / 목(3). 층위가 다르면 이동 짝이 될 수 없다.

    실측(2026-07-23, 개인정보 보호법 시행령 제60조의2): 구①(항)이 통짜
    정의에서 골격+각 호로 재작성되자, LLM 이 구①(항)을 신①1.(호)로
    '이동'시켰다. 항이 호로 이동하는 일은 없다 — 층위로 막는다.
    """
    return sum(1 for p in (item.clause_no, item.item_label, item.subitem_label) if p)


def canon_key(pos: str | None) -> str:
    """위치 라벨 비교용 정규화.

    LLM 은 위치를 '①1' 로, 계약 데이터는 '①1.' 로 쓴다(2026-07-22 실측).
    끝의 마침표와 공백을 정리해 같은 위치를 같게 본다. normalize 와 달리
    번호 자체는 남긴다 — 위치가 같은지 비교하려면 번호가 있어야 한다.
    """
    if not pos:
        return ""
    return re.sub(r"\.$", "", re.sub(r"\s+", "", pos))


def same_position(a: str | None, b: str | None) -> bool:
    return canon_key(a) == canon_key(b)


def content_ratio(a: str, b: str) -> float:
    """두 내용의 유사도(0~1). 정규화 후 비교."""
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def preserved_in(old_text: str, new_text: str) -> bool:
    """구 내용이 신 안에 그대로(부분문자열) 보존됐는가. 비대칭.

    앞에 붙든 뒤에 붙든 중간에 끼든, 구 내용이 신 안에 살아 있으면 True.
    difflib 대칭 유사도와 다르다 — 신 문장이 아무리 길어져도 구 내용만
    보존돼 있으면 True 다. 그래서 "구내용 + 새 단서" 형태를 정확히 잡는다.
    """
    no = normalize(old_text)
    return bool(no) and no in normalize(new_text)


def diff_is_meaningful(old_text: str, new_text: str) -> bool:
    """글자 diff 를 써도 되는가 — '부분 수정'이면 True, '통째 교체'면 False.

    ★ 유사도 하나로는 못 가른다 (2026-07-25 실측):
        · 단서 추가 "…지급한다." → "…지급한다. 다만…"  유사도 0.28,
          그런데 diff 는 완벽(앞부분 그대로 + 뒤에 추가).
        · 통째 교체 "주요재료비…" → "소모재료비…"       유사도 0.46,
          diff 는 무의미한 조각을 낸다.
      유사도로 자르면 0.28(유용)을 버리고 0.46(무의미)을 통과시킨다.

    ★ 진짜 기준은 '앞·중간·뒤 어딘가에 old 가 통째로 보존됐나'다.
        · old 가 new 안에 부분문자열로 남아 있으면 → 단서 추가/삽입류 →
          diff 유용.
        · 아니면서 최장 공통 부분열이 old 의 절반도 안 되면 → 통째 교체 →
          diff 무의미.
    """
    o, n = normalize(old_text or ""), normalize(new_text or "")
    if not o or not n:
        return True
    if o in n or n in o:
        return True  # 한쪽이 다른 쪽에 통째 보존 — 단서 추가/삭제류
    # 최장 공통 부분열이 old 에서 차지하는 비율. 절반 이상이면 부분 수정.
    sm = difflib.SequenceMatcher(None, o, n)
    matched = sum(b.size for b in sm.get_matching_blocks())
    return matched / len(o) >= 0.5


def describe_change(old_text: str, new_text: str, *, max_len: int = 4000) -> list[str]:
    """구/신 문장을 글자단위로 비교해 '바뀐 조각'만 뽑는다.

    실측(국민체육진흥법 제21조): "대한올림픽위원회 → 대한체육회" 처럼
    한 단어만 바뀐 경우, 긴 문장 전체가 아니라 바뀐 부분만 정확히 나온다.

    ★ 단, 항목이 통째로 교체된 경우(diff_is_meaningful=False) 빈 목록을
      돌려준다. 실측(2026-07-25, 예정가격 제9조②2., "부분품비"→"소모공구
      비품비"): 서로 무관한 두 문장을 글자로 대조하니 "'원형대'→'1년미만으'"
      같은 무의미한 조각이 나왔고, LLM 이 그걸 문장으로 만들려다 뒤죽박죽이
      됐다. 빈 목록이면 프롬프트가 old/new 전문을 주는 쪽으로 폴백한다.
    """
    o, n = old_text or "", new_text or ""
    if o and n and not diff_is_meaningful(o, n):
        return []  # 통째 교체 — diff 로 쪼개면 안 된다

    sm = difflib.SequenceMatcher(None, o, n)
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            parts.append(f"'{o[i1:i2]}' → '{n[j1:j2]}'")
        elif tag == "delete":
            parts.append(f"'{o[i1:i2]}' 삭제")
        elif tag == "insert":
            parts.append(f"'{n[j1:j2]}' 추가")
    return [p[:max_len] for p in parts]


def group_by_article(law: LawChange) -> dict[str, list[ArticleDiffItem]]:
    """조문(제○조) 단위로 묶는다. 밀림은 조문 하나 안에서만 일어난다."""
    groups: dict[str, list[ArticleDiffItem]] = defaultdict(list)
    for item in law.articles:
        groups[item.article_label].append(item)
    return dict(groups)


def is_shift_possible(items: list[ArticleDiffItem]) -> bool:
    """이 조문에서 번호 밀림이 일어날 수 있는가.

    신설이 하나도 없으면 밀릴 자리가 없다 — 검사 자체가 불필요하다.
    실측: 74개 조문 그룹 중 12개(16%)만 여기 해당한다.
    """
    has_new = any(i.change_type == "신설" for i in items)
    has_amended = any(i.change_type == "개정" for i in items)
    return has_new and has_amended


# ---------------------------------------------------------------------------
# 핵심: 조문 하나의 구↔신 대응을 결정론적으로 판정
# ---------------------------------------------------------------------------


def resolve_article(
    items: list[ArticleDiffItem],
) -> tuple[list[PositionMapping], list[ArticleDiffItem], list[ArticleDiffItem]]:
    """조문 하나의 대응을 판정한다.

    반환: (확정된 매핑, LLM 이 봐야 할 남은 old, 남은 new)
        남은 old/new 가 비어 있지 않으면 계산으로 다 못 풀었다는 뜻 —
        MappingAgent 가 이 둘만 대조한다.
    """
    resolved: list[PositionMapping] = []
    pool_old: list[ArticleDiffItem] = []  # 자리를 잃은 old
    pool_new: list[ArticleDiffItem] = []  # 출처 없는 new

    for it in items:
        pos = position_label(it)
        has_old = bool((it.old_text or "").strip())
        has_new = bool((it.new_text or "").strip())

        if it.change_type == "삭제":
            pool_old.append(it)
        elif it.change_type == "신설":
            pool_new.append(it)
        elif it.match_status == "성공":
            # 짝을 믿는다 → 제자리 개정
            resolved.append(PositionMapping(pos, pos, "개정", "라벨"))
        elif has_old and has_new and preserved_in(it.old_text, it.new_text):
            # 의심이 붙었지만 구 내용이 제 자리 new 에 보존됨 → 오경보, 제자리 개정
            resolved.append(PositionMapping(pos, pos, "개정", "내용보존"))
        else:
            # 짝을 못 믿는다 → 풀로 (old 와 new 를 따로 넣는다)
            if has_old:
                pool_old.append(it)
            if has_new:
                pool_new.append(it)

    # 1단계: 다른 번호로의 이동 (사라진 내용이 새 자리에 그대로 보존됨).
    #   층위가 같아야 한다 — 항(①)이 호(①1.)로 이동하는 일은 없다.
    used_new: set[int] = set()
    for git in pool_old[:]:
        cand = [
            n for n in pool_new
            if id(n) not in used_new
            and depth(n) == depth(git)
            and not same_position(position_label(git), position_label(n))
            and preserved_in(git.old_text, n.new_text)
        ]
        if len(cand) == 1:
            n = cand[0]
            relation = "이동" if normalize(git.old_text) == normalize(n.new_text) else "이동후개정"
            resolved.append(
                PositionMapping(position_label(git), position_label(n), relation, "내용보존")
            )
            used_new.add(id(n))
            pool_old.remove(git)

    # 2단계: 같은 번호끼리 남은 것 → 제자리 개정 (의심이 틀렸던 것).
    #   단, 내용이 충분히 겹칠 때만 확정한다. 번호는 같아도 내용이 무관하면
    #   (구 내용이 다른 자리로 밀리고 이 자리엔 새 내용이 온 경우) 제자리
    #   개정이 아니다 → 확정하지 말고 풀에 남겨 LLM 이 보게 한다.
    #
    #   실측(2026-07-22, 전자정부법 제56조의2): 구②(위임규정)는 신⑤ 로
    #   밀렸는데 문장이 확장돼("제1항에 따른"→"제1항부터 제4항까지에 따른")
    #   부분문자열로 못 잡았다. 그런데 번호가 같다는 이유로 구②↔신② 를
    #   제자리 개정으로 묶어 LLM 이 볼 기회조차 없었다. 구②↔신② 유사도는
    #   0.32(거의 무관), 구②↔신⑤ 는 0.80. 낮은 쪽을 제자리로 단정하지
    #   않는다 — 애매하면 확정을 보류하고 LLM 에 넘긴다(오판 대신 판단 유보).
    SAME_POS_CONFIRM = 0.5
    for git in pool_old[:]:
        same = [
            n for n in pool_new
            if id(n) not in used_new and same_position(position_label(git), position_label(n))
        ]
        if same and content_ratio(git.old_text, same[0].new_text) >= SAME_POS_CONFIRM:
            n = same[0]
            resolved.append(
                PositionMapping(position_label(git), position_label(n), "개정", "번호일치")
            )
            used_new.add(id(n))
            pool_old.remove(git)

    remaining_new = [n for n in pool_new if id(n) not in used_new]
    return resolved, pool_old, remaining_new


def trivial_mappings(items: list[ArticleDiffItem]) -> list[PositionMapping]:
    """밀림이 불가능한 조문 — 계약의 라벨을 그대로 믿는다.

    신설이 없으면 자리가 흔들릴 일이 없으므로 change_type 이 곧 정답이다.
    """
    result: list[PositionMapping] = []
    for item in items:
        pos = position_label(item)
        if item.change_type == "신설":
            result.append(PositionMapping(None, pos, "신설", basis="라벨"))
        elif item.change_type == "삭제":
            result.append(PositionMapping(pos, None, "삭제", basis="라벨"))
        else:
            result.append(PositionMapping(pos, pos, "개정", basis="라벨"))
    return result
