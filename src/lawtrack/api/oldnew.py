"""신구법 비교조회 (target=oldAndNew / target=admrulOldAndNew).

★ 법령과 행정규칙이 "비교 대상 없음"을 표현하는 방식이 완전히 다르다
   (이 프로젝트 검증 과정에서 가장 늦게, 가장 중요하게 발견된 사실).

    법령 (oldAndNew) — 없어도 정상 JSON 구조가 오고, 필드값으로 표시된다:
        구조문_기본정보 전체가 null/0
        "신구법존재여부": "N"

    행정규칙 (admrulOldAndNew) — 구조 자체가 없고, 문자열 메시지만 온다:
        <Law>일치하는 신구법 없습니다. </Law>
        (JSON 이 아니므로 client 단계에서 resp.data 는 None 이 되고
         resp.text 에 위 문자열이 그대로 담긴다)

   => 법령은 "필드값을 확인"해야 하고, 행정규칙은 "응답이 애초에
      구조화된 JSON 인지"부터 확인해야 한다. 둘을 같은 방식으로
      처리하면 안 된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from lawtrack.api.client import LawApiClient
from lawtrack.parse.jsonutil import collect_texts, dig, find_key, text_of

log = logging.getLogger(__name__)

#: 행정규칙 "없음" 응답에 실제로 등장한 문구.
_ADMRUL_NO_COMPARISON_MARKER = "일치하는 신구법 없습니다"


@dataclass(frozen=True)
class VersionInfo:
    """구조문_기본정보 / 신조문_기본정보."""

    serial_no: str
    source_id: str
    name: str
    enforce_date: str
    promulgation_date: str
    promulgation_no: str
    revision_type: str
    is_current: bool  # 현행여부. 신조문이어도 이후 개정되면 N 이 될 수 있음(실측).


_EMPTY_VERSION = VersionInfo("", "", "", "", "", "", "", False)


@dataclass(frozen=True)
class OldNewResult:
    """신구법 비교조회 결과.

    available=False 인 경우 old/new 관련 필드는 참고용이며, 호출부는
    <P> 추출을 시도하지 말고 "원문 링크만 제공"으로 처리해야 한다
    (그 판단은 parse/oldnew.py 가 아니라 이 결과를 소비하는 파이프라인의
    책임이며, 이 모듈은 사실만 전달한다).
    """

    available: bool
    reason: str  # "" | "no_comparison_field" | "no_comparison_admrul_text"
    old_version: VersionInfo
    new_version: VersionInfo
    old_texts: list[str] = field(default_factory=list)  # 구조문 조각 텍스트(순서 보존)
    new_texts: list[str] = field(default_factory=list)  # 신조문 조각 텍스트(순서 보존)
    raw: dict | None = None


# ---------------------------------------------------------------------------
# 법령
# ---------------------------------------------------------------------------

def fetch_law_oldnew(client: LawApiClient, mst: str) -> OldNewResult:
    """법령 신구법 비교조회.

    실측 예시 (사회보장기본법 시행규칙, 제정 직후):
        구조문_기본정보.법령일련번호 = 0
        구조문_기본정보.법령ID = "null"
        신구법존재여부 = "N"
    """
    resp = client.service(target="oldAndNew", MST=mst)

    if resp.data is None:
        # 예상 밖 — 법령은 JSON 구조로 오는 것이 실측 기본값이었다.
        log.warning("법령 oldAndNew 가 비-JSON 응답으로 옴 (MST=%s): %s", mst, resp.text[:200])
        return OldNewResult(False, "unexpected_non_json", _EMPTY_VERSION, _EMPTY_VERSION)

    data = resp.data
    flag = text_of(find_key(data, "신구법존재여부")).upper()

    old_v = _extract_version(data, "구조문_기본정보", law_like=True)
    new_v = _extract_version(data, "신조문_기본정보", law_like=True)

    if flag == "N":
        return OldNewResult(False, "no_comparison_field", old_v, new_v, raw=data)

    old_texts = collect_texts(dig(data, "구조문목록") or find_key(data, "구조문목록"))
    new_texts = collect_texts(dig(data, "신조문목록") or find_key(data, "신조문목록"))

    return OldNewResult(True, "", old_v, new_v, old_texts, new_texts, raw=data)


# ---------------------------------------------------------------------------
# 행정규칙
# ---------------------------------------------------------------------------

def fetch_admrul_oldnew(client: LawApiClient, rule_serial_no: str) -> OldNewResult:
    """행정규칙 신구법 비교조회.

    실측: 없을 때 JSON 이 아니라 아래 문자열이 그대로 온다.
        <Law>일치하는 신구법 없습니다. </Law>
    이 경우 client 단계에서 JSON 파싱이 애초에 시도되지 않으므로
    resp.data 는 None 이다. 따라서 여기서는 필드가 아니라
    "resp.data 가 None 인지 + 마커 문구가 있는지"로 판정한다.
    """
    resp = client.service(target="admrulOldAndNew", ID=rule_serial_no)

    if resp.data is None:
        if _ADMRUL_NO_COMPARISON_MARKER in resp.text:
            return OldNewResult(False, "no_comparison_admrul_text", _EMPTY_VERSION, _EMPTY_VERSION)
        # 마커도 없고 JSON 도 아니면 진짜 이상 응답 — 조용히 넘기지 않는다.
        log.warning(
            "행정규칙 oldAndNew 응답이 JSON 도 아니고 '없음' 마커도 아님 "
            "(ID=%s): %s", rule_serial_no, resp.text[:200],
        )
        return OldNewResult(False, "unexpected_non_json", _EMPTY_VERSION, _EMPTY_VERSION)

    data = resp.data
    old_v = _extract_version(data, "구조문_기본정보", law_like=False)
    new_v = _extract_version(data, "신조문_기본정보", law_like=False)

    old_texts = collect_texts(dig(data, "구조문목록") or find_key(data, "구조문목록"))
    new_texts = collect_texts(dig(data, "신조문목록") or find_key(data, "신조문목록"))

    return OldNewResult(True, "", old_v, new_v, old_texts, new_texts, raw=data)


# ---------------------------------------------------------------------------
# 내부
# ---------------------------------------------------------------------------

def _extract_version(data: dict, block_key: str, *, law_like: bool) -> VersionInfo:
    block = dig(data, block_key) or find_key(data, block_key) or {}
    if not isinstance(block, dict):
        return _EMPTY_VERSION

    serial_key = "법령일련번호" if law_like else "행정규칙일련번호"
    id_key = "법령ID" if law_like else "행정규칙ID"
    name_key = "법령명" if law_like else "행정규칙명"
    date_key = "공포일자" if law_like else "발령일자"
    no_key = "공포번호" if law_like else "발령번호"

    serial_no = text_of(block.get(serial_key))
    source_id = text_of(block.get(id_key))
    is_current = text_of(block.get("현행여부")).upper() == "Y"

    return VersionInfo(
        serial_no=serial_no,
        source_id="" if source_id.lower() == "null" else source_id,
        name=text_of(block.get(name_key)),
        enforce_date=text_of(block.get("시행일자")),
        promulgation_date=text_of(block.get(date_key)),
        promulgation_no=text_of(block.get(no_key)),
        revision_type=text_of(block.get("제개정구분명")),
        is_current=is_current,
    )