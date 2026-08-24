"""조문 안에서 구(舊) ↔ 신(新) 대응 판정 — 결정론적 부분.

왜 필요한가:
    법제처 신구조문대비표는 개정 전/후를 '순서'로만 짝짓음. 항이 중간에
    신설되면 뒤 항들이 밀리는데, 대비표는 그걸 모른 채 자리끼리 나란히
    놓음. 그래서 계약 JSON 의 change_type/짝짓기가 실제와 어긋남.

    실측(범죄피해자 보호법 제47조):
        실제:  구① → 신②,  구② → 신④,  신①③ 이 진짜 신설
        JSON:  신④ = "신설"(밀려온 기존 조항인데), 신② = "개정"(밀림인데)

    이 라벨을 그대로 요약하면 "④항이 신설되었습니다"라는 잘못된 메일이
    나감. 담당자는 없던 처벌 조항이 생긴 줄 앎.

판정 원칙 (번호를 기준으로 삼음):
    번호가 양쪽에 다 있고 짝을 믿을 수 있으면 → 제자리 개정. 이동 아님.
    번호가 한쪽에만 있거나 짝이 의심스러우면 → '풀'에 모아 내용으로 대조:
        사라진 내용이 다른 번호의 새 항목에 그대로 있으면 → 이동
        같은 번호끼리 남으면 → 제자리 개정(의심이 틀렸던 것)
        끝내 안 맞으면 → 삭제 / 신설

    임계값(유사도 0.7 등)을 쓰지 않음. 임계값은 표본에서 뽑은 숫자라
    새 법령에서 조용히 틀림. "부분문자열로 그대로 보존"은 표본과 무관하게
    참임. 계산으로 확정 못 하는 소수(문장이 확장되며 옮겨간 경우)만
    MappingAgent(LLM)로 넘김.

    실측: 12개 조문 그룹 중 11개가 이 계산만으로 정답.
    나머지 1개(전자정부법 제56조의2, "제1항에 따른"→"제1항부터 제4항까지에
    따른" 으로 확장되며 이동)만 부분문자열로 못 잡아 LLM 이 필요함.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict

from lawtrack.contract.schema import ArticleDiffItem, LawChange

from summarizer.models import PositionMapping
from summarizer.textdiff import tokenize

# 문장 앞의 항/호/목 번호. 내용 비교 전에 떼어냄 — 번호가 밀리면 이
# 부분만 달라지므로, 붙여둔 채 비교하면 같은 내용을 다른 것으로 봄.
_LEADING_LABEL = re.compile(r"^\s*(?:[0-9]+\.|[가-힣]\.|[①-⑳])\s*")


def normalize(text: str) -> str:
    """내용 비교용 정규화 — 앞 번호 제거 + 공백 전부 제거."""
    return re.sub(r"\s+", "", _LEADING_LABEL.sub("", text or ""))


def position_label(item: ArticleDiffItem) -> str:
    """조문 안에서의 위치. 예: '①7.'"""
    return "".join(p for p in (item.clause_no, item.item_label, item.subitem_label) if p)


def depth(item: ArticleDiffItem) -> int:
    """항(1) / 호(2) / 목(3). 층위가 다르면 이동 짝이 될 수 없음.

    실측(개인정보 보호법 시행령 제60조의2): 구①(항)이 통짜
    정의에서 골격+각 호로 재작성되자, LLM 이 구①(항)을 신①1.(호)로
    '이동'시켰음. 항이 호로 이동하는 일은 없음 — 층위로 막음.
    """
    return sum(1 for p in (item.clause_no, item.item_label, item.subitem_label) if p)


def canon_key(pos: str | None) -> str:
    """위치 라벨 비교용 정규화.

    LLM 은 위치를 '①1' 로, 계약 데이터는 '①1.' 로 씀(실측).
    끝의 마침표와 공백을 정리해 같은 위치를 같게 봄. normalize 와 달리
    번호 자체는 남김 — 위치가 같은지 비교하려면 번호가 있어야 함.
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
    difflib 대칭 유사도와 다름 — 신 문장이 아무리 길어져도 구 내용만
    보존돼 있으면 True 다. 그래서 "구내용 + 새 단서" 형태를 정확히 잡음.
    """
    no = normalize(old_text)
    return bool(no) and no in normalize(new_text)


def diff_is_meaningful(old_text: str, new_text: str) -> bool:
    """글자 diff 를 써도 되는가 — '부분 수정'이면 True, '통째 교체'면 False.

    유사도 하나로는 못 가름 (실측):
        · 단서 추가 "…지급한다." → "…지급한다. 다만…"  유사도 0.28,
          그런데 diff 는 완벽(앞부분 그대로 + 뒤에 추가).
        · 통째 교체 "주요재료비…" → "소모재료비…"       유사도 0.46,
          diff 는 무의미한 조각을 냄.
      유사도로 자르면 0.28(유용)을 버리고 0.46(무의미)을 통과시킴.

    진짜 기준은 '앞·중간·뒤 어딘가에 old 가 통째로 보존됐나'다.
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


_MERGE_GAP_WORDS = 3
"""바뀐 조각 두 개 사이에 안 바뀐 어절이 이 개수 이하로 끼어 있으면 하나로
합침.

실측(전기사업법 제7조의3① "제3항에 따라 미리 관계
행정기관의 장과 협의한" → "인ㆍ허가등의 관계 행정기관의 장과 미리
협의한"): "미리"라는 단어가 문장 안에서 자리만 옮겨가면, SequenceMatcher는
이걸 '이동'으로 보지 못하고 "'제3항에 따라 미리' → '인ㆍ허가등의'"(replace)와
"' 미리' 추가"(insert) 두 조각으로 따로 뽑음. 두 조각을 각각 따로 받은
LLM은 "미리"가 왜 지워졌다가 다시 생기는지 맥락을 모른 채 "인ㆍ허가등의
미리를 추가하였습니다" 같은 비문을 만들었음. 사이에 낀 안 바뀐 어절
("관계 행정기관의 장과", 3어절)이 짧으면 같은 구절 안에서 일어난 하나의
재구성일 가능성이 높으므로, 통째로 합쳐 "'제3항에 따라 미리 관계
행정기관의 장과' → '인ㆍ허가등의 관계 행정기관의 장과 미리'"처럼 하나의
완결된 치환으로 줌.

3으로 잡은 이유: "20일 이내 또는 그 행위가 있음을 안 날부터 15일"(제28조②
실측)처럼 서로 무관한 두 변경 사이에 낀 어절은 보통 7개 이상이라, 이 값
이하에서는 두 독립된 변경을 잘못 하나로 합칠 위험이 낮음.
"""


def _merge_close_opcodes(
    opcodes: list[tuple[str, int, int, int, int]], a: list[str],
) -> list[tuple[str, int, int, int, int]]:
    """가까이 붙은(사이에 낀 안 바뀐 어절이 적은) 변경 조각들을 하나로 합침.

    SequenceMatcher.get_opcodes()는 non-equal(replace/delete/insert) 조각들
    사이에 항상 equal 조각을 하나씩 두고 나옴(둘이 붙어 있으면 애초에
    replace 하나로 합쳐서 나옴) — 그래서 "change, equal(짧음), change"
    패턴만 살피면 됨.
    """
    ops = list(opcodes)
    merged: list[tuple[str, int, int, int, int]] = []
    i = 0
    n = len(ops)
    while i < n:
        tag, i1, i2, j1, j2 = ops[i]
        if tag == "equal":
            merged.append(ops[i])
            i += 1
            continue

        cur_i1, cur_i2, cur_j1, cur_j2 = i1, i2, j1, j2
        i += 1
        while i + 1 < n and ops[i][0] == "equal" and ops[i + 1][0] != "equal":
            gap_i1, gap_i2 = ops[i][1], ops[i][2]
            gap_words = len([t for t in a[gap_i1:gap_i2] if t.strip()])
            if gap_words > _MERGE_GAP_WORDS:
                break
            cur_i2 = ops[i + 1][2]
            cur_j2 = ops[i + 1][4]
            i += 2

        if cur_i1 == cur_i2:
            final_tag = "insert"
        elif cur_j1 == cur_j2:
            final_tag = "delete"
        else:
            final_tag = "replace"
        merged.append((final_tag, cur_i1, cur_i2, cur_j1, cur_j2))
    return merged


def describe_change(old_text: str, new_text: str, *, max_len: int = 4000) -> list[str]:
    """구/신 문장을 어절단위로 비교해 '바뀐 조각'만 뽑음.

    실측(국민체육진흥법 제21조): "대한올림픽위원회 → 대한체육회" 처럼
    한 단어만 바뀐 경우, 긴 문장 전체가 아니라 바뀐 부분만 정확히 나옴.

    실측(국가를 당사자로 하는 계약에 관한 법률 제28조②
    "…20일 이내…15일 이내…" → "…30일 이내…25일 이내…"): 예전엔 글자
    단위(difflib.SequenceMatcher(None, o, n))로 비교했는데, "20일"과
    "30일"은 앞 숫자 한 글자만 다르고 "0일"은 같아서 SequenceMatcher가
    "'2' → '3'"처럼 숫자 한 글자만 조각으로 뽑아버렸음("15일"→"25일"도
    "'1' → '2'"로 동일). 단위(單位)를 잃은 맨 숫자만 LLM에게 던지니,
    LLM이 그 숫자가 뭘 가리키는지 지어내 "당사자의 수가 2에서 3으로"
    같은 완전히 무관한 문장을 만들어냈음(구체적으로 쓰라는 프롬프트
    규칙과 맞물려 없는 맥락을 채워 넣은 것). 어절(단어) 단위로 바꾸면
    "20일"/"30일" 전체가 한 조각으로 나와 이 문제가 생기지 않음.
    summarizer.textdiff.tokenize()는 이미 같은 이유(한글 글자단위 diff의
    무의미한 쪼개짐)로 어절 토큰화를 쓰고 있어 그대로 재사용함.

    단, 항목이 통째로 교체된 경우(diff_is_meaningful=False) 빈 목록을
      돌려줌. 실측(예정가격 제9조②2., "부분품비"→"소모공구
      비품비"): 서로 무관한 두 문장을 글자로 대조하니 "'원형대'→'1년미만으'"
      같은 무의미한 조각이 나왔고, LLM 이 그걸 문장으로 만들려다 뒤죽박죽이
      됐음. 빈 목록이면 프롬프트가 old/new 전문을 주는 쪽으로 폴백함.
    """
    o, n = old_text or "", new_text or ""
    if o and n and not diff_is_meaningful(o, n):
        return []  # 통째 교체 — diff 로 쪼개면 안 된다

    a, b = tokenize(o), tokenize(n)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    opcodes = _merge_close_opcodes(sm.get_opcodes(), a)
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "replace":
            parts.append(f"'{''.join(a[i1:i2])}' → '{''.join(b[j1:j2])}'")
        elif tag == "delete":
            parts.append(f"'{''.join(a[i1:i2])}' 삭제")
        elif tag == "insert":
            parts.append(f"'{''.join(b[j1:j2])}' 추가")

    # 실측(산업재해보상보험법 제116조③ "증명을 생략할 수
    # 있다" → "증명 또는 자료의 제공을 생략할 수 있다"): 같은 조각("
    # 또는 자료의 제공' 추가")이 문장 안 서로 다른 두 곳에 각각 삽입되면
    # 완전히 같은 문자열이 목록에 두 번 나옴. LLM이 이걸 "왜 두 번
    # 나오지?"로 받아들여 "~항목이 두 번 언급되었습니다" 같은, 내용과
    # 무관한 문장을 지어낸 적이 있음. 위치 정보까지 줄 필요는 없고,
    # 중복을 하나로 합쳐 "(N곳)"만 표시하면 충분함 — 문자열 자체는
    # 그대로 두 곳 모두에 적용된다는 뜻이 이미 통함.
    counts = Counter(parts)
    deduped: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(f"{p} ({counts[p]}곳)" if counts[p] > 1 else p)

    return [p[:max_len] for p in deduped]


def group_by_article(law: LawChange) -> dict[str, list[ArticleDiffItem]]:
    """조문(제○조) 단위로 묶음. 밀림은 조문 하나 안에서만 일어남."""
    groups: dict[str, list[ArticleDiffItem]] = defaultdict(list)
    for item in law.articles:
        groups[item.article_label].append(item)
    return dict(groups)


def is_shift_possible(items: list[ArticleDiffItem]) -> bool:
    """이 조문에서 번호 밀림이 일어날 수 있는가.

    신설이 하나도 없으면 밀릴 자리가 없음 — 검사 자체가 불필요함.
    실측: 74개 조문 그룹 중 12개(16%)만 여기 해당함.
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
    """조문 하나의 대응을 판정함.

    반환: (확정된 매핑, LLM 이 봐야 할 남은 old, 남은 new)
        남은 old/new 가 비어 있지 않으면 계산으로 다 못 풀었다는 뜻 —
        MappingAgent 가 이 둘만 대조함.
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
            # 짝을 못 믿는다 → 풀로 (old 와 new 를 따로 넣음)
            if has_old:
                pool_old.append(it)
            if has_new:
                pool_new.append(it)

    # 1단계: 다른 번호로의 이동 (사라진 내용이 새 자리에 그대로 보존됨).
    #   층위가 같아야 함 — 항(①)이 호(①1.)로 이동하는 일은 없음.
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
    #   단, 내용이 충분히 겹칠 때만 확정함. 번호는 같아도 내용이 무관하면
    #   (구 내용이 다른 자리로 밀리고 이 자리엔 새 내용이 온 경우) 제자리
    #   개정이 아니다 → 확정하지 말고 풀에 남겨 LLM 이 보게 함.
    #
    #   실측(전자정부법 제56조의2): 구②(위임규정)는 신⑤ 로
    #   밀렸는데 문장이 확장돼("제1항에 따른"→"제1항부터 제4항까지에 따른")
    #   부분문자열로 못 잡았음. 그런데 번호가 같다는 이유로 구②↔신② 를
    #   제자리 개정으로 묶어 LLM 이 볼 기회조차 없었음. 구②↔신② 유사도는
    #   0.32(거의 무관), 구②↔신⑤ 는 0.80. 낮은 쪽을 제자리로 단정하지
    #   않음 — 애매하면 확정을 보류하고 LLM 에 넘김(오판 대신 판단 유보).
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
    """밀림이 불가능한 조문 — 계약의 라벨을 그대로 믿음.

    신설이 없으면 자리가 흔들릴 일이 없으므로 change_type 이 곧 정답임.
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
