"""국가법령정보 Open API HTTP 클라이언트.

모든 API 호출은 반드시 이 모듈을 거친다. 호출부마다 각자 검증하면
아래와 같은 사고가 난다.

--------------------------------------------------------------------------
[실측된 치명적 버그 — 조용한 데이터 오염]

    인증 실패 시 API 는 다음을 반환한다:

        HTTP 200 OK
        {"result": "사용자 정보 검증에 실패하였습니다.",
         "msg": "OPEN API 호출 시 사용자 검증을 위하여 정확한 서버장비의
                 IP주소 및 도메인주소를 등록해 주세요."}

    이 응답은
        response.raise_for_status()  → HTTP 200 이므로 통과
        response.json()              → 유효한 JSON 이므로 통과
        isinstance(data, dict)       → True 이므로 통과
    세 검사를 전부 통과한다.

    결과: 에러 메시지가 '법령 전문'으로 DB 에 저장되고, 콘솔에는
          "[법령 성공]" 이 찍힌다. JSON_LENGTH() 가 0 이 아니게 되므로
          재적재 대상에서도 빠져 영구 복구 불가.

    => 내용 기반 검증(_check_api_error)이 반드시 필요하다.
--------------------------------------------------------------------------

[예외 설계]
    LawApiAuthError  : 인증/IP 문제. 재시도 무의미 → 즉시 중단
    LawApiHttpError  : 네트워크/HTTP 오류. 재시도 대상
    LawApiFormatError: 응답 구조 이상. 재시도 무의미
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from lawtrack.config import ApiSettings
from lawtrack.parse.jsonutil import looks_like_api_error

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------

class LawApiError(RuntimeError):
    """API 호출 관련 기반 예외."""

    def __init__(self, message: str, *, url: str = "", body: str = ""):
        super().__init__(message)
        self.url = url
        self.body = body[:500]


class LawApiAuthError(LawApiError):
    """인증키/IP 등록 문제. 재시도해도 소용없다."""


class LawApiHttpError(LawApiError):
    """네트워크/HTTP 계층 오류. 재시도 대상."""


class LawApiFormatError(LawApiError):
    """응답이 기대한 형식이 아님."""


# ---------------------------------------------------------------------------
# 응답
# ---------------------------------------------------------------------------

@dataclass
class ApiResponse:
    """API 응답 래퍼.

    JSON 파싱 실패가 곧 에러는 아니다. 예를 들어 행정규칙 신구법이 없으면
    구조화된 JSON 이 아니라 아래 텍스트가 온다(실측):

        <Law>일치하는 신구법 없습니다. </Law>

    이는 정상적인 업무 응답이므로 client 에서 예외를 던지지 않고,
    호출부(api/oldnew.py)가 판정하도록 text 를 그대로 넘긴다.
    """

    url: str
    status_code: int
    content_type: str
    text: str
    data: dict | None = None

    @property
    def is_json(self) -> bool:
        return self.data is not None

    def json_or_raise(self) -> dict:
        if self.data is None:
            raise LawApiFormatError(
                f"JSON 응답이 아닙니다. Content-Type={self.content_type}",
                url=self.url,
                body=self.text,
            )
        return self.data


# ---------------------------------------------------------------------------
# 클라이언트
# ---------------------------------------------------------------------------

#: 인증 실패 응답에 등장하는 문구(실측). 부분일치로 탐지한다.
_AUTH_ERROR_MARKERS = (
    "사용자 정보 검증에 실패",
    "IP주소 및 도메인주소를 등록",
    "인증키가 유효하지",
)

#: 전문 응답이라면 최소 이 정도 길이는 나온다. 지나치게 짧으면 이상 신호.
_MIN_FULLTEXT_CHARS = 300


class LawApiClient:
    """국가법령정보 API 클라이언트.

    사용:
        client = LawApiClient(settings.api)
        resp = client.search(target="law", query="국민기초생활보장법")
        resp = client.service(target="law", MST="276653")
    """

    def __init__(self, settings: ApiSettings, session: httpx.Client | None = None):
        self._s = settings
        self._session = session or httpx.Client(verify=False)
        self._session.headers.update({"User-Agent": settings.user_agent})
        self._last_call_at = 0.0

    # -- public ------------------------------------------------------------

    def search(self, *, target: str, **params: Any) -> ApiResponse:
        """목록조회(lawSearch.do).

        display 는 호출부가 무엇을 넘기든 최대치로 강제한다.
        (에너지법 사례처럼 정답이 순위 밖으로 밀리는 것을 막기 위함)
        """
        params = {**params, "target": target, "display": self._s.display}
        return self._request(self._s.search_url, params)

    def service(self, *, target: str, **params: Any) -> ApiResponse:
        """본문조회(lawService.do).

        파라미터가 대상별로 다르다(실측):
            법령      → MST=법령일련번호
            행정규칙  → ID=행정규칙일련번호
        분기는 호출부(api/fulltext.py)가 담당한다.
        """
        params = {**params, "target": target}
        return self._request(self._s.service_url, params)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "LawApiClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- internal ----------------------------------------------------------

    def _request(self, url: str, params: dict) -> ApiResponse:
        params = {"OC": self._s.oc, "type": self._s.response_type, **params}
        last_exc: Exception | None = None

        for attempt in range(1, self._s.max_retries + 1):
            self._throttle()
            try:
                resp = self._once(url, params)
                if attempt > 1:
                    log.info("재시도 성공 (%d회차): %s", attempt, self._safe_url(url, params))
                return resp

            except LawApiAuthError:
                # 인증/IP 문제는 재시도해도 동일하다. 즉시 전파.
                raise

            except LawApiFormatError:
                # 구조 이상도 재시도 대상 아님.
                raise

            except (LawApiHttpError, httpx.RequestError) as exc:
                last_exc = exc
                if attempt >= self._s.max_retries:
                    break
                wait = self._s.backoff_base * (2 ** (attempt - 1))
                log.warning(
                    "호출 실패(%d/%d), %.1fs 후 재시도: %s",
                    attempt, self._s.max_retries, wait, exc,
                )
                time.sleep(wait)

        raise LawApiHttpError(
            f"{self._s.max_retries}회 재시도 후에도 실패: {last_exc}",
            url=self._safe_url(url, params),
        )

    def _once(self, url: str, params: dict) -> ApiResponse:
        try:
            r = self._session.get(url, params=params, timeout=self._s.timeout)
        except httpx.RequestError as exc:
            raise LawApiHttpError(f"네트워크 오류: {exc}", url=url) from exc

        if r.status_code >= 500:
            raise LawApiHttpError(f"서버 오류 HTTP {r.status_code}", url=r.url, body=r.text)
        if r.status_code >= 400:
            # 4xx 는 재시도해도 대개 동일하지만, 429(rate limit)만 예외로 재시도.
            if r.status_code == 429:
                raise LawApiHttpError("요청 한도 초과(429)", url=r.url, body=r.text)
            raise LawApiFormatError(f"HTTP {r.status_code}", url=r.url, body=r.text)

        text = r.text or ""
        content_type = r.headers.get("Content-Type", "")

        # 1) 텍스트 레벨 인증 오류 탐지 (JSON/XML 양쪽 모두 커버)
        self._check_auth_error(text, r.url)

        # 2) JSON 파싱 시도. 실패해도 예외를 던지지 않는다.
        data: dict | None = None
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                data = parsed if isinstance(parsed, dict) else None
            except ValueError:
                data = None

        # 3) JSON 레벨 에러 구조 탐지
        if data is not None and looks_like_api_error(data):
            raise LawApiAuthError(
                f"API 에러: {data.get('result')} / {data.get('msg')}",
                url=r.url,
                body=text,
            )

        return ApiResponse(
            url=r.url,
            status_code=r.status_code,
            content_type=content_type,
            text=text,
            data=data,
        )

    @staticmethod
    def _check_auth_error(text: str, url: str) -> None:
        head = text[:1000]
        for marker in _AUTH_ERROR_MARKERS:
            if marker in head:
                raise LawApiAuthError(
                    "인증 실패로 보이는 응답입니다. OC 키와 서버 IP/도메인 등록을 확인하세요.",
                    url=url,
                    body=text,
                )

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call_at
        if gap < self._s.rate_limit_sleep:
            time.sleep(self._s.rate_limit_sleep - gap)
        self._last_call_at = time.monotonic()

    def _safe_url(self, url: str, params: dict) -> str:
        """로그용. OC 키를 마스킹한다."""
        masked = {**params, "OC": "***"}
        query = "&".join(f"{k}={v}" for k, v in masked.items())
        return f"{url}?{query}"


# ---------------------------------------------------------------------------
# 전문 응답 검증
# ---------------------------------------------------------------------------

def assert_fulltext_payload(data: dict, *, context: str = "") -> None:
    """본문조회 응답이 실제 전문인지 최소 검증.

    루트 키 이름을 하드코딩하지 않는 이유:
        본 프로젝트의 API 검증은 전부 type=XML 로 수행되었고,
        type=JSON 응답의 루트 키를 실측으로 확정하지 못했다.
        예측을 코드에 박으면 틀렸을 때 전 건이 막힌다.

    대신 '에러가 아님 + 내용이 충분히 큼' 으로 방어하고,
    실제 루트 키는 아래 로그로 확인한 뒤 확정한다.
    """
    if looks_like_api_error(data):
        raise LawApiAuthError(f"{context}: API 에러 응답 {data}")

    size = len(json.dumps(data, ensure_ascii=False))
    if size < _MIN_FULLTEXT_CHARS:
        raise LawApiFormatError(
            f"{context}: 전문 응답이 비정상적으로 짧습니다({size}자). "
            f"루트키={list(data)[:5]}"
        )

    log.debug("%s 응답 루트키=%s (%d자)", context or "fulltext", list(data)[:5], size)