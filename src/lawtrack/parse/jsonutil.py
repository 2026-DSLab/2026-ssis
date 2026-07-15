"""JSON 응답 파싱 유틸.

핵심 문제 (팀 전수검증 실측):
    국가법령정보 API 를 type=JSON 으로 호출하면, 반복 요소가
      - 결과 1건  → dict
      - 결과 2건+ → list
    로 다르게 온다. XML 에서는 항상 반복 요소지만 JSON 변환 과정에서
    단일 항목이 배열로 감싸지지 않기 때문이다.

    admrul 실측 분포:
        단일(dict) 21건 (87%)  /  복수(list) 3건 (13%)

    => list 를 전제로 파서를 짜면 87% 케이스에서 터진다.
       모든 반복 요소 접근에 as_list() 를 반드시 통과시킨다.

이 규칙은 목록조회뿐 아니라 전문 JSON 내부(조문단위, 항, 호, 목)에도
동일하게 적용된다. 조문이 1개인 법령은 dict 로 온다.
"""

from __future__ import annotations

from typing import Any


def as_list(value: Any) -> list:
    """반복 요소를 항상 list 로 정규화한다.

    >>> as_list(None)
    []
    >>> as_list({"a": 1})
    [{'a': 1}]
    >>> as_list([{"a": 1}, {"a": 2}])
    [{'a': 1}, {'a': 2}]

    주의: 문자열은 감싸기만 하고 문자 단위로 쪼개지 않는다.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def dig(data: Any, *keys: str, default: Any = None) -> Any:
    """중첩 dict 안전 접근.

    >>> dig({"a": {"b": {"c": 1}}}, "a", "b", "c")
    1
    >>> dig({"a": {}}, "a", "b", "c", default="-")
    '-'

    중간에 list 를 만나면 첫 요소로 내려간다. API 응답이 단일/복수를
    오가는 특성 때문에, 경로 탐색 중에도 방어가 필요하다.
    """
    cur = data
    for key in keys:
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def dig_list(data: Any, *keys: str) -> list:
    """중첩 접근 후 as_list. 반복 요소 탐색의 표준 진입점."""
    return as_list(dig(data, *keys))


def text_of(value: Any, default: str = "") -> str:
    """값에서 문자열 본문을 뽑는다.

    API 응답은 값이 그냥 문자열인 경우와, {"#text": "..."} 처럼
    감싸인 경우가 섞일 수 있으므로 양쪽을 처리한다.
    CDATA 로 인해 앞뒤 공백이 붙어 오는 경우가 많아 strip 한다.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("#text", "content", "value", "_"):
            if key in value:
                return text_of(value[key], default)
        return default
    if isinstance(value, list):
        return text_of(value[0], default) if value else default
    return default


def first_key_of(data: Any, candidates: tuple[str, ...]) -> str | None:
    """후보 키 중 실제 존재하는 첫 키를 반환.

    법령/행정규칙 JSON 의 루트 키 이름을 아직 실측으로 확정하지 못했다.
    (본 프로젝트의 API 검증은 전부 type=XML 로 수행됨)
    하드코딩하면 예측이 틀렸을 때 전부 막히므로, 후보군을 두고 탐색한다.
    실제 응답을 한 번 확인한 뒤 후보군을 확정할 것.
    """
    if not isinstance(data, dict):
        return None
    for key in candidates:
        if key in data:
            return key
    return None


def find_key(data: Any, key: str) -> Any:
    """중첩 구조 어디에 있든 첫 번째로 발견되는 key 의 값을 반환.

    JSON 루트 구조를 실측으로 확정하지 못한 상태에서 특정 필드
    (예: 신구법존재여부)의 정확한 경로를 예측해 하드코딩하면, 예측이
    틀렸을 때 전부 막힌다. 대신 트리를 순회해 키 이름으로 찾는다.

    실제 경로가 확정되면 이 탐색 대신 dig() 로 교체하는 것이 더 빠르고
    안전하다 — 이 함수는 과도기적 방어책이다.
    """
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            found = find_key(v, key)
            if found is not None:
                return found
        return None
    if isinstance(data, list):
        for item in data:
            found = find_key(item, key)
            if found is not None:
                return found
        return None
    return None


def collect_texts(node: Any, text_keys: tuple[str, ...] = ("content", "text", "#text", "value")) -> list[str]:
    """중첩 구조에서 텍스트 리프(leaf)들을 순서대로 수집.

    구조문목록/신조문목록처럼 정확한 경로가 불확실한 반복 요소에서
    본문 텍스트만 순서대로 뽑아낼 때 쓴다. dict 에서 text_keys 중
    하나를 발견하면 그 문자열을 잎으로 채택하고 더 깊이 내려가지 않는다
    (중복 수집 방지).
    """
    out: list[str] = []
    if isinstance(node, dict):
        for tk in text_keys:
            val = node.get(tk)
            if isinstance(val, str) and val.strip():
                out.append(val)
                return out
        for v in node.values():
            out.extend(collect_texts(v, text_keys))
        return out
    if isinstance(node, list):
        for item in node:
            out.extend(collect_texts(item, text_keys))
        return out
    return out


def looks_like_api_error(data: Any) -> bool:
    """API 레벨 에러 응답인지 판정.

    실측된 에러 응답 (HTTP 200 + 유효 JSON 으로 온다):
        {"result": "사용자 정보 검증에 실패하였습니다.",
         "msg": "OPEN API 호출 시 사용자 검증을 위하여 …"}

    raise_for_status() 도 json() 도 isinstance(dict) 도 전부 통과하므로,
    내용으로 판정하지 않으면 이 값이 '전문'으로 DB 에 저장된다.
    """
    if not isinstance(data, dict):
        return False
    keys = set(data.keys())
    if {"result", "msg"} <= keys and len(keys) <= 3:
        return True
    return False