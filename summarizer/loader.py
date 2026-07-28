"""계약 JSON 로드 + 조문 단위 정규화.

전부 결정론적이다 — LLM 이 개입하지 않는다. 이 경계를 지키는 것이 중요한
이유: "무엇이 바뀌었는지"는 <P> 태그와 6가드가 이미 확정한 사실이고,
LLM 의 역할은 그 사실을 문장으로 다듬는 것뿐이다.
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

from summarizer.matching import canon_key, describe_change, normalize
from summarizer.models import ArticleMapping, ArticleUnit, PositionMapping


def load_contract(path: str | Path) -> WeeklyContract:
    """계약 JSON 을 읽어 Pydantic 모델로 검증한다.

    여기서 검증하는 이유: 필드가 빠진 채로 LLM 까지 흘러가면 원인 모를
    요약 오류가 난다. 계약 위반은 계약 경계에서 터뜨린다.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return WeeklyContract.model_validate(raw)


def iter_laws(contract: WeeklyContract) -> list[LawChange]:
    """계약에서 법령 목록을 평평하게 뽑는다.

    amendment_groups(공포번호 그룹)는 "정부조직 개편 일괄정비" 같은 맥락을
    담고 있지만, 법령 단위 요약 자체는 LawChange 하나로 완결되므로 여기서는
    편다. 그룹 맥락이 필요해지면 group.is_chained 를 2단계 프롬프트에
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
        # '성공'이 아닌 모든 match_status 는 old_text 를 신뢰할 수 없다는 뜻이다.
        # 계약 스키마의 match_status 설명이 케이스별로 그 이유를 적어 두었다.
        old_text_is_context=item.match_status != "성공",
    )


def _units_from_expansion(law: LawChange, group: StructuralExpansion) -> list[ArticleUnit]:
    """구조확장 그룹 하나를 새로 생긴 위치 개수만큼의 유닛으로 쪼갠다.

    old_text 는 그룹 전체의 참고 맥락이므로 모든 유닛에 같은 값이 복제되고,
    old_text_is_context 는 항상 True 다 — 구법에 이 세부 위치가 없었으니
    대응하는 개정 전 문장이 애초에 존재하지 않는다.
    """
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
    """법령 1건을 1단계 에이전트가 먹을 유닛 목록으로 정규화한다."""
    units = [_unit_from_article(law, item) for item in law.articles]
    for group in law.structural_expansions:
        units.extend(_units_from_expansion(law, group))
    return units


def apply_mappings(
    units: list[ArticleUnit], mappings: list["ArticleMapping"]
) -> list[ArticleUnit]:
    """매핑 판정 결과로 조문 라벨을 교정한다.

    ★ 왜 1단계 '앞'에서 해야 하는가 (2026-07-21 실측):
        처음에는 매핑을 2단계에만 넘겼다. 그러자 2단계가 서로 모순된 두
        정보를 받게 됐다 — 1단계 요약은 계약의 틀린 라벨대로 "①7 개정,
        ①8 신설"이라 말하고, 매핑은 "①7 신설, 구①7이 ①8로 이동"이라
        말했다. 프롬프트에 "매핑이 정답"이라고 적어뒀지만 모델이 틀린
        쪽을 택했고, 결국 잘못된 요약이 나왔다.

        모순된 정보를 주고 고르게 하면 안 된다. 여기서 미리 교정해서
        1단계부터 올바른 라벨로 요약하게 하면, 뒤 단계에는 일관된 정보만
        흘러간다.
    """
    # 위치(조문+항호목)로 매핑을 찾는다. LLM 은 '①1', unit 은 '제18조①1.'
    # 로 쓰므로 canon_key 로 맞춘다(2026-07-22 실측: 안 맞추면 교정 누락).
    by_new: dict[str, PositionMapping] = {}
    for article in mappings:
        for m in article.mappings:
            if m.new_position:
                by_new[canon_key(article.article_label + m.new_position)] = m

    # 이동후개정의 diff 를 위해 '밀려온 원래 자리의 old 내용'을 찾아야 한다.
    old_by_pos = {canon_key(u.location_label): u.old_text for u in units if u.old_text}
    # 조문 라벨 접두어를 붙여 조회하려면 unit 의 조문 라벨이 필요한데
    # location_label = 조문 + 위치 이므로, moved_from(위치만)로 찾으려면
    # 같은 조문 안에서 위치 접미사로 매칭한다.
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
            # 아니다(밀려나기 전 다른 항목의 문장). 지운다 — 남겨두면
            # LLM 이 "A에서 B로 바뀌었다"고 쓴다.
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
            # 내용 그대로 번호만 이동. LLM 불필요 — 규칙으로 서술.
            corrected.append(
                replace(
                    unit,
                    change_type="이동",
                    moved_from=m.old_position,
                    old_text_is_context=False,
                    label_corrected=True,
                    move_is_identical=True,
                )
            )
        elif m.relation == "이동후개정":
            # 옮겨가며 내용도 바뀜. 원래 자리의 old 와 diff 를 낸다.
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
            # 매핑이 '같은 자리 실제 개정'으로 확정했다. 세 갈래로 나눈다:
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
    """match_status 에서 신뢰도 경고를 뽑는다. 규칙으로 확정 — LLM 미개입."""
    if not unit.old_text_is_context:
        return []
    return [
        f"[{unit.location_label}] {unit.match_status} — "
        "개정 전 문장이 이 위치에 정확히 대응하지 않음"
    ]
