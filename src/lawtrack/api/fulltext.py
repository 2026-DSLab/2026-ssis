"""본문조회 (target=law / target=admrul).

법령과 행정규칙은 상세조회 파라미터 이름이 다르다 (실측):
    법령      : lawService.do?...&target=law&MST=법령일련번호
    행정규칙  : lawService.do?...&target=admrul&ID=행정규칙일련번호

이 차이를 잘못 맞추면(예: 행정규칙에 MST= 를 쓰면) 엉뚱한 응답이 오거나
빈 응답이 오는데, 겉보기엔 '호출은 성공'했기 때문에 원인 파악이 늦어진다.
그래서 이 모듈에서 파라미터 분기를 강제하고, 호출부는 law_id 계열인지
admrul_id 계열인지만 신경 쓰면 되게 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from lawtrack.api.client import ApiResponse, LawApiClient, assert_fulltext_payload
from lawtrack.parse.jsonutil import text_of

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FullTextResult:
    """본문조회 결과. 원본 JSON(raw)을 그대로 보존해 DB의 *_full_text
    JSON 컬럼에 저장할 수 있게 한다. 파싱된 필드는 파이프라인 판단용."""

    raw: dict
    serial_no: str
    source_id: str          # 법령ID 또는 행정규칙ID
    name: str
    revision_reason: str    # 제개정이유 (LLM 이 추론할 필요 없게 함)
    revision_text: str      # 개정문 (관보 공포문, 줄글)


def fetch_law_fulltext(client: LawApiClient, mst: str) -> FullTextResult:
    """법령 본문조회. target=law&MST=..."""
    resp = client.service(target="law", MST=mst)
    data = resp.json_or_raise()
    assert_fulltext_payload(data, context=f"law MST={mst}")
    return _build_law_result(data, mst)


def fetch_admrul_fulltext(client: LawApiClient, rule_serial_no: str) -> FullTextResult:
    """행정규칙 본문조회. target=admrul&ID=행정규칙일련번호.

    주의: 여기서의 ID 는 행정규칙ID 가 아니라 행정규칙일련번호다.
    목록조회 응답의 상세링크가 이를 실측으로 확인해준다.
        /DRF/lawService.do?...&target=admrul&ID=2100000243290
    """
    resp = client.service(target="admrul", ID=rule_serial_no)
    data = resp.json_or_raise()
    assert_fulltext_payload(data, context=f"admrul ID={rule_serial_no}")
    return _build_admrul_result(data, rule_serial_no)


# ---------------------------------------------------------------------------
# 내부 파싱
# ---------------------------------------------------------------------------
# ✅ 루트 키 실측 확인됨(2026-07-16, 법령 5건·행정규칙 3건 교차검증):
#   법령 본문조회(target=law)     → "법령" (매번 일관됨)
#   행정규칙 본문조회(target=admrul) → "AdmRulService" (매번 일관됨)
# 후보 리스트는 만약을 대비해 유지하되, 확인된 키를 맨 앞에 둔다.

def _find_root(data: dict, candidates: tuple[str, ...]) -> dict:
    for key in candidates:
        if key in data and isinstance(data[key], dict):
            return data[key]
    return data  # 후보에 없으면 최상위를 그대로 사용 (평탄한 구조일 가능성)


def _build_law_result(data: dict, mst: str) -> FullTextResult:
    root = _find_root(data, ("법령", "Law", "LawService"))

    basic = root.get("기본정보", root)
    law_id = text_of(basic.get("법령ID") or root.get("법령ID"))
    name = text_of(basic.get("법령명_한글") or basic.get("법령명한글") or root.get("법령명한글"))

    reason = _extract_nested_text(root, "제개정이유", "제개정이유내용")
    revision_text = _extract_nested_text(root, "개정문", "개정문내용")

    return FullTextResult(
        raw=data,
        serial_no=mst,
        source_id=law_id,
        name=name,
        revision_reason=reason,
        revision_text=revision_text,
    )


def _build_admrul_result(data: dict, serial_no: str) -> FullTextResult:
    # ✅ 실측 확인(2026-07-16, 여러 ID 교차검증): 실제 루트 키는 "AdmRulService"
    # 뿐이다. "행정규칙"/"AdmRul"은 확인된 적 없는 추측 후보였고, 매번 이
    # 뒤의 세 번째 후보(AdmRulService)로만 성공해왔다. 실측 키를 앞에 두어
    # 불필요한 순회를 없앤다.
    root = _find_root(data, ("AdmRulService", "행정규칙", "AdmRul"))

    basic = root.get("기본정보", root)
    rule_id = text_of(basic.get("행정규칙ID") or root.get("행정규칙ID"))
    name = text_of(basic.get("행정규칙명") or root.get("행정규칙명"))

    # 행정규칙은 개정문/제개정이유 필드 존재 여부가 법령과 다를 수 있어
    # 있으면 채우고 없으면 빈 문자열로 둔다 (assert 하지 않음).
    reason = _extract_nested_text(root, "제개정이유", "제개정이유내용")
    revision_text = _extract_nested_text(root, "개정문", "개정문내용")

    return FullTextResult(
        raw=data,
        serial_no=serial_no,
        source_id=rule_id,
        name=name,
        revision_reason=reason,
        revision_text=revision_text,
    )


def _flatten_lines(node) -> list[str]:
    lines: list[str] = []
    if isinstance(node, str):
        stripped = node.strip()
        if stripped:
            lines.append(stripped)
    elif isinstance(node, list):
        for item in node:
            lines.extend(_flatten_lines(item))
    return lines


def _extract_nested_text(root: dict, outer_key: str, inner_key: str) -> str:
    """"제개정이유"/"개정문" 처럼 한 겹 더 안에 실제 내용이 있는 필드를 읽는다.

    ★★ 실측 발견(2026-07-16, 공공기관의 정보공개에 관한 법률 등): root의
    "제개정이유"/"개정문" 키는 그 자체가 문자열이 아니라
    {"제개정이유내용": [[...줄들...]]} / {"개정문내용": [[...줄들...]]}
    형태로 한 겹 더 감싸여 있다. 예전엔 _dig_any(root, ("제개정이유",
    "제개정이유내용"))가 이 둘을 "같은 레벨의 후보"로 취급해, 실제로는
    항상 "제개정이유"(딕셔너리)가 먼저 걸려 그 딕셔너리 자체를
    text_of()에 넘겼는데, text_of()는 dict에서 "#text"/"content"/
    "value"/"_" 키만 볼 뿐이라 늘 빈 문자열로 떨어졌다 — schema.py 가
    강조하는 revision_reason 필드가 이 때문에 한 번도 채워진 적이 없었다.
    내용 자체는 별표내용과 같은 "줄 단위 문자열 배열" 형식이라 평탄화해
    공백으로 이어붙인다.
    """
    outer = root.get(outer_key)
    if not isinstance(outer, dict):
        return text_of(outer)  # 혹시 예전 가정대로 평문으로 오는 경우 대비
    return " ".join(_flatten_lines(outer.get(inner_key)))