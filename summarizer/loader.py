"""계약 JSON 로드 + 조문 단위 정규화.

전부 결정론적임 — LLM 이 개입하지 않음. 이 경계를 지키는 것이 중요한
이유: "무엇이 바뀌었는지"는 <P> 태그와 6가드가 이미 확정한 사실이고,
LLM 의 역할은 그 사실을 문장으로 다듬는 것뿐임.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from lawtrack.contract.schema import (
    ArticleDiffItem,
    LawChange,
    StructuralExpansion,
    WeeklyContract,
)

from summarizer.matching import _LEADING_LABEL, canon_key, content_ratio, describe_change, normalize
from summarizer.models import ArticleMapping, ArticleUnit, PositionMapping

#: "이동"(내용 그대로 번호만 이동) 판정을 실제로 믿어도 되는 유사도 하한.
#: matching.py resolve_article()가 이미 실측(전자정부법
#: 제56조의2)으로 이 정확한 케이스를 분석해뒀음 — 완전일치는 코드가
#: 못 풀고(문장이 확장됨), LLM 판정으로 넘어간 것 자체가 이미 "애매함"의
#: 신호임. 그런데 LLM이 "이동"이라 답하면 이 함수가 그걸 그대로 믿고
#: move_is_identical=True 를 줘서 검증·요약을 통째로 건너뛰고 있었음.
#: 실측: 그 LLM 판정 자체가 note 에는 "재구성됨"이라 써놓고도
#: relation 은 "이동"을 골라 자기모순인 응답을 낸 사례가 실제로 나왔음
#: (해당 쌍의 content_ratio=0.80 — 진짜 동일 이동 사례들은 전부 1.0에
#: 가까움). LLM의 "이동" 주장을 곧이곧대로 믿지 않고, 여기서 코드로
#: 한 번 더 확인함 — 이 프로젝트 전체의 원칙("LLM 자기 판단을 검증에
#: 쓰지 않는다")과 같은 이유임.
_MOVE_IDENTICAL_MIN_RATIO = 0.95


def load_contract(path: str | Path) -> WeeklyContract:
    """계약 JSON 을 읽어 Pydantic 모델로 검증함.

    여기서 검증하는 이유: 필드가 빠진 채로 LLM 까지 흘러가면 원인 모를
    요약 오류가 남. 계약 위반은 계약 경계에서 터뜨림.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return WeeklyContract.model_validate(raw)


def iter_laws(contract: WeeklyContract) -> list[LawChange]:
    """계약에서 법령 목록을 평평하게 뽑음.

    amendment_groups(공포번호 그룹)는 "정부조직 개편 일괄정비" 같은 맥락을
    담고 있지만, 법령 단위 요약 자체는 LawChange 하나로 완결되므로 여기서는
    폄. 그룹 맥락이 필요해지면 group.is_chained 를 2단계 프롬프트에
    넘기는 방식으로 확장할 것.
    """
    return [law for group in contract.amendment_groups for law in group.laws]


def _unit_from_article(law: LawChange, item: ArticleDiffItem) -> ArticleUnit:
    return ArticleUnit(
        law_id=law.law_id,
        law_name=law.law_name,
        location_label=item.location_label,
        change_type=item.change_type,
        old_text=item.old_text,
        new_text=item.new_text,
        match_status=item.match_status,
        # '성공'이 아닌 모든 match_status 는 old_text 를 신뢰할 수 없다는 뜻임.
        # 계약 스키마의 match_status 설명이 케이스별로 그 이유를 적어 두었음.
        old_text_is_context=item.match_status != "성공",
    )


#: _find_content_match() 가 "진짜 대응"으로 인정할 최소 정규화 길이.
#: 실측(전자정부법 제2조11. "정보자원" 정의): 개정 전
#: 프로즈("행정정보, ... 정보시스템, ... 정보기술, 정보화예산 및
#: 정보화인력 등을 말한다")가 개정 후 가.나.다.라.마.바.목으로 쪼개졌는데,
#: 가("행정정보")·나("정보시스템")·다("정보시스템의 구축에 적용되는
#: 정보기술")·마("정보화예산")·바("정보화인력") 5개는 프로즈 안에 그대로
#: (부분)문자열로 남아 있어 확정할 수 있었지만, 라("정보시스템의 운영에
#: 필요한 건축물 및 건축설비...")는 프로즈 어디에도 없는 완전히 새 내용
#: 이었음 — 이런 경우는 매칭하면 안 됨. "부분문자열이면 확정"이라는
#: 규칙 하나로는, 아주 짧은 조각(예: "정보")이 우연히 여기저기 걸릴
#: 오탐 위험이 있어 최소 길이를 둠. 4는 실측에서 나온 가장 짧은 진짜
#: 매치("행정정보")의 길임 — 이보다 짧은 매치는 오탐 위험이 더 크다고
#: 보고 확정하지 않음(대신 기존처럼 "구조확장" 미확정으로 남김).
_MIN_EXPANSION_MATCH_LEN = 4


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """normalize()와 같은 규칙(앞 번호 제거 + 공백 제거)으로 정규화하되,
    정규화된 문자열의 각 글자가 원문의 몇 번째 인덱스였는지도 같이 돌려줌.

    매치를 찾은 뒤 원문 그대로(공백ㆍ서식 보존)의 조각을 잘라내려면 이
    역매핑이 필요함 — normalize() 단독으로는 매치 위치를 원문으로
    되돌릴 수 없음.
    """
    text = text or ""
    m = _LEADING_LABEL.match(text)
    start = m.end() if m else 0
    chars: list[str] = []
    index_map: list[int] = []
    for i in range(start, len(text)):
        ch = text[i]
        if ch.isspace():
            continue
        chars.append(ch)
        index_map.append(i)
    return "".join(chars), index_map


def _find_content_match(
    old_text: str, item_text: str, consumed: list[bool],
) -> str | None:
    """item_text(정규화)가 old_text(정규화) 안에 그대로 있으면, 원문
    그대로의 해당 조각을 잘라 돌려줌. 없으면 None.

    consumed: old_text 정규화 문자열과 길이가 같은 리스트. 이미 다른
    항목이 근거로 쓴 구간은 True로 표시해, 서로 다른 두 새 항목이 같은
    옛 조각 하나를 중복해서 근거로 삼지 않게 함(실측 사례엔 없었지만,
    짧은 단어가 반복되는 조문에서 안전장치로 필요함).
    """
    old_norm, old_map = _normalize_with_map(old_text)
    item_norm, _ = _normalize_with_map(item_text)
    if len(item_norm) < _MIN_EXPANSION_MATCH_LEN:
        return None

    search_from = 0
    while True:
        pos = old_norm.find(item_norm, search_from)
        if pos == -1:
            return None
        span = range(pos, pos + len(item_norm))
        if not any(consumed[k] for k in span):
            for k in span:
                consumed[k] = True
            start_i, end_i = old_map[pos], old_map[pos + len(item_norm) - 1]
            return old_text[start_i : end_i + 1]
        search_from = pos + 1


def _units_from_expansion(law: LawChange, group: StructuralExpansion) -> list[ArticleUnit]:
    """구조확장 그룹 하나를 새로 생긴 위치 개수만큼의 유닛으로 쪼갬.

    예전엔 모든 유닛을 "구조확장(구법미분리)"으로만
    표시했음(옛 프로즈 문장이 항목별로 안 나뉘어 있어 어차피 대응을
    확정 못 한다는 전제). 그런데 실측해보니 그 전제가 항상 참은 아니었음
    — 프로즈가 쉼표로 나열된 목록이었고, 그 목록 문구가 새 항목 문구
    안에 그대로 남아 있는 경우가 있었음(위 _MIN_EXPANSION_MATCH_LEN
    주석 참고). 이런 항목은 old_text 전체가 아니라 실제로 대응하는
    조각만 잘라 match_status="성공"으로 확정함 — 나머지(진짜 새 내용)
    만 예전처럼 미확정으로 남김.
    """
    old_norm, _ = _normalize_with_map(group.old_text)
    consumed = [False] * len(old_norm)

    units: list[ArticleUnit] = []
    for new_item in group.new_items:
        label = "".join(
            p
            for p in (
                group.article_label,
                new_item.clause_no,
                new_item.item_label,
                new_item.subitem_label,
            )
            if p
        )
        matched = _find_content_match(group.old_text, new_item.text, consumed)
        if matched is not None:
            units.append(
                ArticleUnit(
                    law_id=law.law_id,
                    law_name=law.law_name,
                    location_label=label,
                    change_type="개정",
                    old_text=matched,
                    new_text=new_item.text,
                    match_status="성공",
                    old_text_is_context=False,
                )
            )
            continue
        units.append(
            ArticleUnit(
                law_id=law.law_id,
                law_name=law.law_name,
                location_label=label,
                change_type="신설",
                old_text=group.old_text,
                new_text=new_item.text,
                match_status="구조확장(구법미분리)",
                old_text_is_context=True,
            )
        )
    return units


def build_article_units(law: LawChange) -> list[ArticleUnit]:
    """법령 1건을 1단계 에이전트가 먹을 유닛 목록으로 정규화함."""
    units = [_unit_from_article(law, item) for item in law.articles]
    for group in law.structural_expansions:
        units.extend(_units_from_expansion(law, group))
    return units


def apply_mappings(
    units: list[ArticleUnit], mappings: list["ArticleMapping"]
) -> list[ArticleUnit]:
    """매핑 판정 결과로 조문 라벨을 교정함.

    왜 1단계 '앞'에서 해야 하는가 (실측):
        처음에는 매핑을 2단계에만 넘겼음. 그러자 2단계가 서로 모순된 두
        정보를 받게 됐음 — 1단계 요약은 계약의 틀린 라벨대로 "①7 개정,
        ①8 신설"이라 말하고, 매핑은 "①7 신설, 구①7이 ①8로 이동"이라
        말했음. 프롬프트에 "매핑이 정답"이라고 적어뒀지만 모델이 틀린
        쪽을 택했고, 결국 잘못된 요약이 나왔음.

        모순된 정보를 주고 고르게 하면 안 됨. 여기서 미리 교정해서
        1단계부터 올바른 라벨로 요약하게 하면, 뒤 단계에는 일관된 정보만
        흘러감.
    """
    # 위치(조문+항호목)로 매핑을 찾음. LLM 은 '①1', unit 은 '제18조①1.'
    # 로 쓰므로 canon_key 로 맞춤(실측: 안 맞추면 교정 누락).
    by_new: dict[str, PositionMapping] = {}
    for article in mappings:
        for m in article.mappings:
            if m.new_position:
                by_new[canon_key(article.article_label + m.new_position)] = m

    # 이동후개정의 diff 를 위해 '밀려온 원래 자리의 old 내용'을 찾아야 함.
    old_by_pos = {canon_key(u.location_label): u.old_text for u in units if u.old_text}
    # 조문 라벨 접두어를 붙여 조회하려면 unit 의 조문 라벨이 필요한데
    # location_label = 조문 + 위치 이므로, moved_from(위치만)로 찾으려면
    # 같은 조문 안에서 위치 접미사로 매칭함.
    old_by_suffix = {u.location_label: u.old_text for u in units if u.old_text}

    def _find_moved_old(moved_from: str | None) -> str:
        if not moved_from:
            return ""
        mk = canon_key(moved_from)
        for label, text in old_by_suffix.items():
            if canon_key(label).endswith(mk):
                return text
        return ""

    corrected: list[ArticleUnit] = []
    for unit in units:
        m = by_new.get(canon_key(unit.location_label))

        if m is None or m.relation == "동일":
            corrected.append(unit)
            continue

        if m.relation == "신설":
            # 계약이 '개정'이라 표시하며 붙여둔 old_text 는 이 위치의 것이
            # 아님(밀려나기 전 다른 항목의 문장). 지움 — 남겨두면
            # LLM 이 "A에서 B로 바뀌었다"고 씀.
            corrected.append(
                replace(
                    unit,
                    change_type="신설",
                    old_text="",
                    old_text_is_context=False,
                    label_corrected=unit.change_type != "신설",
                )
            )
        elif m.relation == "이동":
            # 내용 그대로 번호만 이동 — 이라고 매핑이 주장하지만, basis가
            # "LLM판정"인 경우 그 주장 자체가 틀릴 수 있음(위 상수 설명
            # 참고). 코드로 실제 유사도를 한 번 더 확인한 뒤에만 믿음.
            moved_old = _find_moved_old(m.old_position)
            if moved_old and content_ratio(moved_old, unit.new_text) < _MOVE_IDENTICAL_MIN_RATIO:
                corrected.append(
                    replace(
                        unit,
                        change_type="이동후개정",
                        moved_from=m.old_position,
                        old_text=moved_old,
                        old_text_is_context=False,
                        label_corrected=True,
                        diff_parts=describe_change(moved_old, unit.new_text),
                    )
                )
            else:
                # 실측 발견(공공기관 데이터베이스
                # 표준화 지침 제16조⑤): 여기서도 old_text 를 moved_old 로
                # 바꿔야 하는데 빠져 있었음 — unit.old_text 는 여전히
                # "이 새 위치와 같은 번호였던 옛 위치"의 텍스트(순수
                # 위치 기준 매칭)라, 밀려서 번호가 바뀐 경우 완전히
                # 다른 조문 내용이 붙어 있었음. "내용 변경 없이 이동"이라는
                # 요약 자체는 맞는데(content_ratio 로 이미 확인됨),
                # "원문 보기"에 뜨는 개정 전 텍스트가 실제로 비교한
                # 문장이 아니라 엉뚱한 옛 자리 문장이었음.
                corrected.append(
                    replace(
                        unit,
                        change_type="이동",
                        moved_from=m.old_position,
                        old_text=moved_old,
                        old_text_is_context=False,
                        label_corrected=True,
                        move_is_identical=True,
                    )
                )
        elif m.relation == "이동후개정":
            # 옮겨가며 내용도 바뀜. 원래 자리의 old 와 diff 를 냄.
            moved_old = _find_moved_old(m.old_position)
            corrected.append(
                replace(
                    unit,
                    change_type="이동후개정",
                    moved_from=m.old_position,
                    old_text=moved_old,
                    old_text_is_context=False,
                    label_corrected=True,
                    diff_parts=describe_change(moved_old, unit.new_text) if moved_old else [],
                )
            )
        else:  # 개정 (제자리)
            # 매핑이 '같은 자리 실제 개정'으로 확정했음. 세 갈래로 나눔:
            #   변경 없음(old==new)  → no_change (같은 조문 다른 항만 바뀐 경우)
            #   diff 유의미           → 바뀐 조각만
            #   통째 교체             → 전문 폴백(is_whole_replace)
            if normalize(unit.old_text) == normalize(unit.new_text):
                corrected.append(
                    replace(unit, change_type="개정", old_text_is_context=False, no_change=True)
                )
            else:
                parts = describe_change(unit.old_text, unit.new_text)
                corrected.append(
                    replace(
                        unit,
                        change_type="개정",
                        old_text_is_context=False,
                        diff_parts=parts,
                        is_whole_replace=not parts,  # diff 가 비었으면 통째 교체
                    )
                )

    return corrected


def caveats_for(unit: ArticleUnit) -> list[str]:
    """match_status 에서 신뢰도 경고를 뽑음. 규칙으로 확정 — LLM 미개입.

    설계: match_status 기반 캐비어트를
    전부 없앴음. 이 함수는 아래 순서로 세 번 좁혀 봤음 —
    "삭제(위치탐색제외)"(확실한 삭제) 제외 → "구조확장(구법미분리)"
    (내용 대조 매칭을 이미 시도해 확정된 신설) 제외 → 위치재배치의심도
    content_ratio 높으면 제외 → change_type="신설"이면 무조건 제외.
    그런데 매번 좁힌 뒤에도 "이것도 사실 문제없는데?"인 실측 사례가
    계속 나왔음(예: 산업재해보상보험법 제127조④ — 옛 내용이 실은
    ④1.으로 토씨 하나 안 틀리고 그대로 옮겨갔는데도 부모 항목이
    위치재배치의심으로 남아 있었음). "위치재배치의심" 자체가 원래
    설계부터 "과잉 표시가 거짓 확정보다 안전하다"는 원칙으로 만들어져
    (src/lawtrack/db/repo.py:_reshuffled_articles 참고) 구조적으로
    노이즈가 신호보다 훨씬 많음 — 매번 사례별로 막는 대신, 이 경고
    자체를 신뢰할 만한 신호로 보지 않기로 했음.

    verify_summaries()(코드로 원문과 직접 대조하는 환각·방향오류 검사,
    설계상 오탐이 날 수 없음)는 그대로 "확인이 필요한 항목"에 남음 —
    match_status는 "짝 배정이 불확실할 수도 있다"는 구조적 신호일
    뿐이지만, verifier는 "요약이 원문과 실제로 어긋난다"는 사실 확인이라
    종류가 다름. match_status 원문 값 자체는 article_diff DB 컬럼에
    그대로 남아 있으니, 필요하면 나중에 그 값으로 다시 무언가를 만들 수
    있음 — 여기서 화면에 안 보여줄 뿐 데이터를 지우는 게 아님.
    """
    return []
