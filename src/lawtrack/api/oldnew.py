"""신구법 비교조회 (target=oldAndNew / target=admrulOldAndNew).

★ 법령과 행정규칙 둘 다 "비교 대상 없음"을 표현하는 방식이 여러 가지다
   (이 프로젝트 검증 과정에서 여러 차례 갱신된 사실 — 아래는 2026-07-18
   기준 최신 확인 내용).

    법령 (oldAndNew) — 정상 JSON 구조로 오고, 필드값으로 표시된다:
        구조문_기본정보 전체가 null/0
        "신구법존재여부": "N"

    행정규칙 (admrulOldAndNew) — 세 가지 형태가 모두 실측됨:
        (1) 구조 자체가 없고 문자열 메시지만 오는 경우:
            <Law>일치하는 신구법 없습니다. </Law>
            (JSON 이 아니므로 client 단계에서 resp.data 는 None 이 되고
             resp.text 에 위 문자열이 그대로 담긴다)
        (2) 같은 메시지가 {"Law": "일치하는 신구법 없습니다."} 형태의
            *유효한 JSON*으로 오는 경우 (resp.data 는 None 이 아님)
        (3) ★★★ 실측 발견(2026-07-18, (계약예규) 공동계약운용요령 등):
            법령과 똑같이 "신구법존재여부": "N" 필드를 가진 정상 JSON
            구조로 오는 경우도 있다(구조문목록/신조문목록 키 자체가 없음).
            이전엔 "행정규칙은 필드로 안 온다"고 단정해 이 케이스를
            놓쳤었다 — 실측으로 반증됨.

   => 행정규칙은 위 세 형태를 전부 확인해야 하며, 그중 어느 하나라도
      맞으면 "비교 불가"로 처리해야 한다. 하나만 확인하고 나머지를
      "비교 가능"으로 오판하면, old_texts/new_texts 가 빈 리스트인 채로
      "available=True" 가 나가는 조용한 오답이 생긴다(겉보기엔 "0건
      변경"과 구분이 안 되지만 의미가 다르다).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from lawtrack.api.client import LawApiClient
from lawtrack.parse.jsonutil import collect_texts, dig, find_key, text_of

log = logging.getLogger(__name__)

#: 행정규칙 "없음" 응답에 실제로 등장한 문구.
_ADMRUL_NO_COMPARISON_MARKER = "일치하는 신구법 없습니다"


def _unwrap_root(data: dict, key: str) -> dict:
    """✅ 실측 확인됨(2026-07-16): 신구법 비교조회 응답은 항상 서비스명
    키(법령="OldAndNewService", 행정규칙="AdmRulOldAndNewService") 한 겹
    아래에 구조문_기본정보/신조문_기본정보/구조문목록/신조문목록이 들어있다.
    이 함수 도입 전에는 이 사실을 몰라 dig()가 매번 실패하고 find_key()의
    전체 트리 재귀탐색으로만 값을 찾아왔다(동작은 했지만 비효율적)."""
    inner = data.get(key)
    return inner if isinstance(inner, dict) else data


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
    root = _unwrap_root(data, "OldAndNewService")
    flag = text_of(root.get("신구법존재여부") or find_key(data, "신구법존재여부")).upper()

    old_v = _extract_version(root, "구조문_기본정보", law_like=True)
    new_v = _extract_version(root, "신조문_기본정보", law_like=True)

    if flag == "N":
        return OldNewResult(False, "no_comparison_field", old_v, new_v, raw=data)

    old_texts = collect_texts(dig(root, "구조문목록") or find_key(data, "구조문목록"))
    new_texts = collect_texts(dig(root, "신조문목록") or find_key(data, "신조문목록"))

    return OldNewResult(True, "", old_v, new_v, old_texts, new_texts, raw=data)


# ---------------------------------------------------------------------------
# 행정규칙
# ---------------------------------------------------------------------------

def fetch_admrul_oldnew(client: LawApiClient, rule_serial_no: str) -> OldNewResult:
    """행정규칙 신구법 비교조회.

    실측: 없을 때 보통은 JSON 이 아니라 아래 문자열이 그대로 온다.
        <Law>일치하는 신구법 없습니다. </Law>
    이 경우 client 단계에서 JSON 파싱이 애초에 시도되지 않으므로
    resp.data 는 None 이다. 따라서 여기서는 필드가 아니라
    "resp.data 가 None 인지 + 마커 문구가 있는지"로 판정한다.

    ★★ 실측 발견(2026-07-16, 하도급거래공정화 지침·중소기업자간 경쟁제품
    직접생산 확인기준): 같은 "없음" 메시지가 비-JSON 텍스트가 아니라
    {"Law": "일치하는 신구법 없습니다."} 형태의 *유효한 JSON*으로 오는
    경우도 있다. 이때는 resp.data 가 None 이 아니므로 위 분기를 그냥
    통과해버려 "available=True, old_texts=[], new_texts=[]" 라는 잘못된
    결과(신구법이 없는데 비교 가능하다고 오판)가 나왔었다 — 구조문목록/
    신조문목록이 원래 없는 응답이라 그 자체로는 검색 실패도 안 나고
    diff_count=0으로 "조용히" 넘어가 원인 파악이 어려웠다. resp.data가
    JSON이어도 이 마커 문구가 있으면 마찬가지로 없음 처리한다.

    ★★★ 실측 발견(2026-07-18, (계약예규) 공동계약운용요령·중소 소프트웨어
    사업자의 사업 참여 지원에 관한 지침): 이 모듈 맨 위 docstring이 "행정규칙은
    구조 자체가 없고 문자열 메시지만 온다"고 단정했던 것 자체가 틀렸다 —
    법령과 완전히 동일한 `신구법존재여부: "N"` 필드를 정상 JSON 구조로
    돌려주는 admrul 응답이 실제로 있다(raw: {"신구법존재여부":"N",
    "구조문_기본정보":{...},"신조문_기본정보":{...}} — 구조문목록/신조문목록
    키 자체가 아예 없음). 이 함수는 지금까지 텍스트 마커 2종(비-JSON/JSON내
    "Law" 메시지)만 확인하고 이 필드는 전혀 안 봤다 — law쪽
    fetch_law_oldnew는 이미 이 필드를 확인하는데 admrul쪽만 빠져 있었다.
    그 결과 "available=True, old_texts=[], new_texts=[]"로 잘못 통과되어,
    실제로는 "신구법 대비 불가"인 두 건이 "비교했는데 진짜 0건 변경"으로
    둔갑했다 — 겉보기엔 무해해 보이지만(둘 다 결과적으로 "0건") 의미가
    다르다: 전자는 "이 개정은 검토가 필요하다"는 신호이고 후자는 "정말
    아무것도 안 바뀌었다"는 신호다. law와 동일하게 필드부터 확인한다.
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
    if _ADMRUL_NO_COMPARISON_MARKER in text_of(find_key(data, "Law")):
        return OldNewResult(False, "no_comparison_admrul_json", _EMPTY_VERSION, _EMPTY_VERSION)
    root = _unwrap_root(data, "AdmRulOldAndNewService")
    flag = text_of(root.get("신구법존재여부") or find_key(data, "신구법존재여부")).upper()
    if flag == "N":
        old_v = _extract_version(root, "구조문_기본정보", law_like=False)
        new_v = _extract_version(root, "신조문_기본정보", law_like=False)
        return OldNewResult(False, "no_comparison_field", old_v, new_v, raw=data)
    old_v = _extract_version(root, "구조문_기본정보", law_like=False)
    new_v = _extract_version(root, "신조문_기본정보", law_like=False)

    old_texts = collect_texts(dig(root, "구조문목록") or find_key(data, "구조문목록"))
    new_texts = collect_texts(dig(root, "신조문목록") or find_key(data, "신조문목록"))

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