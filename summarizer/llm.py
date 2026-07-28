"""LLM 호출 경계.

★ 여기가 실제 Anthropic API 를 때리는 유일한 파일이다.
   에이전트들은 LLMClient 프로토콜만 보고, 어떤 구현체가 꽂혔는지 모른다.
   덕분에 API 키 없이도 DryRunClient 로 파이프라인 전체를 돌려볼 수 있다.

필요:
    pip install anthropic
    .env 에 ANTHROPIC_API_KEY=sk-ant-...
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from summarizer.config import LLMSettings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 호출 실패."""


class LLMClient(Protocol):
    """에이전트가 보는 유일한 인터페이스."""

    def complete_text(
        self, *, system: str, user: str, max_tokens: int, thinking: bool = False
    ) -> str:
        """평문 응답을 받는다. (1단계용)"""
        ...

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        thinking: bool = False,
    ) -> dict:
        """JSON 스키마를 강제해 dict 로 받는다. (2단계용)"""
        ...


# ---------------------------------------------------------------------------
# 실제 구현체
# ---------------------------------------------------------------------------


class AnthropicClient:
    """Anthropic Messages API 클라이언트.

    설계 메모:
      - 응답에서 text 블록을 '찾아서' 꺼낸다. thinking 을 켜면 thinking
        블록이 먼저 오기 때문에 content[0] 을 그냥 집으면 깨진다.
      - 2단계는 structured outputs(output_config.format)로 JSON 스키마를
        강제한다. 프롬프트로 "JSON 으로 답해줘"라고 부탁하는 것보다
        안전하다 — 파싱 실패가 구조적으로 사라진다.
      - 재시도는 SDK 가 429/5xx 에 대해 알아서 한다. 여기서 다시 감싸지 않는다.
    """

    def __init__(self, settings: LLMSettings):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "anthropic 패키지가 없습니다. `pip install anthropic` 를 실행하세요."
            ) from exc

        self._settings = settings
        self._client = anthropic.Anthropic(
            api_key=settings.api_key,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
        )

    def _base_kwargs(self, *, max_tokens: int, thinking: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "max_tokens": max_tokens,
        }
        if thinking:
            # adaptive — 모델이 필요한 만큼만 생각한다.
            # budget_tokens 방식은 Opus 4.7+ 에서 제거됐다(400 에러).
            kwargs["thinking"] = {"type": "adaptive"}
        if self._settings.effort:
            kwargs["output_config"] = {"effort": self._settings.effort}
        return kwargs

    @staticmethod
    def _first_text(response: Any) -> str:
        """응답에서 첫 text 블록을 꺼낸다.

        thinking 블록이 앞에 올 수 있으므로 반드시 type 으로 걸러야 한다.
        """
        for block in response.content:
            if block.type == "text":
                return block.text
        raise LLMError(
            f"응답에 text 블록이 없습니다 (stop_reason={response.stop_reason})"
        )

    @staticmethod
    def _check_stop(response: Any) -> None:
        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            category = getattr(detail, "category", None) if detail else None
            raise LLMError(f"모델이 응답을 거부했습니다 (category={category})")
        if response.stop_reason == "max_tokens":
            raise LLMError(
                "max_tokens 에 걸려 응답이 잘렸습니다. "
                "SUMMARY_*_MAX_TOKENS 를 올리세요."
            )

    def complete_text(
        self, *, system: str, user: str, max_tokens: int, thinking: bool = False
    ) -> str:
        response = self._client.messages.create(
            system=system,
            messages=[{"role": "user", "content": user}],
            **self._base_kwargs(max_tokens=max_tokens, thinking=thinking),
        )
        self._check_stop(response)
        return self._first_text(response).strip()

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        thinking: bool = False,
    ) -> dict:
        kwargs = self._base_kwargs(max_tokens=max_tokens, thinking=thinking)
        # effort 를 이미 넣었을 수 있으므로 output_config 를 덮어쓰지 않고 병합한다.
        output_config = kwargs.pop("output_config", {})
        output_config["format"] = {"type": "json_schema", "schema": schema}

        response = self._client.messages.create(
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
            **kwargs,
        )
        self._check_stop(response)
        text = self._first_text(response)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise LLMError(f"JSON 파싱 실패: {text[:200]!r}") from exc


class OpenAIClient:
    """OpenAI Chat Completions 클라이언트 (gpt-5.4-mini 등).

    AnthropicClient 와 똑같은 LLMClient 프로토콜을 만족한다 — 파이프라인·
    에이전트·프롬프트는 어느 쪽이 꽂혔는지 모른다. 원내 QWEN 으로 옮길 때도
    이 자리에 QwenClient 를 하나 더 만들면 되고 나머지는 그대로다.

    ⚠️ 확인 필요:
        max_tokens 파라미터 이름이 모델 세대에 따라 다르다(구형은
        max_tokens, 신형은 max_completion_tokens). 어느 쪽인지 확실하지
        않아 실패 시 자동으로 바꿔 재시도하게 해뒀다. 실제로 한 번 돌려
        보고 로그를 확인할 것.
    """

    def __init__(self, settings: LLMSettings):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "openai 패키지가 없습니다. `pip install openai` 를 실행하세요."
            ) from exc

        self._settings = settings
        kwargs = {
            "api_key": settings.api_key,
            "timeout": settings.timeout,
            "max_retries": settings.max_retries,
        }
        if settings.base_url:
            # OpenRouter 등 OpenAI 호환 중계 서비스용
            kwargs["base_url"] = settings.base_url
        self._client = openai.OpenAI(**kwargs)
        self._token_param = "max_completion_tokens"
        self._json_mode: str | None = None

        log.info(
            "LLM: %s / model=%s%s",
            settings.provider,
            settings.model,
            f" / base_url={settings.base_url}" if settings.base_url else "",
        )

    def _create(self, *, system: str, user: str, max_tokens: int, response_format=None):
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs = {"model": self._settings.model, "messages": messages}
        if response_format is not None:
            kwargs["response_format"] = response_format

        for param in (self._token_param, "max_tokens", "max_completion_tokens"):
            try:
                return self._client.chat.completions.create(**kwargs, **{param: max_tokens})
            except Exception as exc:  # 파라미터 이름 문제만 넘기고 나머지는 던진다
                if "max_tokens" not in str(exc) and "max_completion_tokens" not in str(exc):
                    raise LLMError(str(exc)) from exc
                log.debug("토큰 파라미터 %s 거부됨 — 다른 이름으로 재시도", param)
                continue
        raise LLMError("max_tokens / max_completion_tokens 둘 다 거부되었습니다.")

    @staticmethod
    def _text_of(response) -> str:
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise LLMError(
                "출력 토큰 한도에 걸려 응답이 잘렸습니다. SUMMARY_*_MAX_TOKENS 를 올리세요."
            )
        content = choice.message.content
        if not content:
            raise LLMError(f"빈 응답 (finish_reason={choice.finish_reason})")
        return content

    def complete_text(
        self, *, system: str, user: str, max_tokens: int, thinking: bool = False
    ) -> str:
        # thinking 은 Anthropic 전용 개념이라 여기서는 무시한다.
        # 프로토콜을 맞추기 위해 인자만 받는다.
        return self._text_of(
            self._create(system=system, user=user, max_tokens=max_tokens)
        ).strip()

    @staticmethod
    def _strip_fence(text: str) -> str:
        """```json ... ``` 감싸기 제거.

        스키마 강제 모드가 아니면 모델이 마크다운 코드블록으로 감싸는 일이
        흔하다. 그대로 json.loads 하면 실패한다.
        """
        t = text.strip()
        if t.startswith("```"):
            t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
            t = re.sub(r"\s*```$", "", t)
        return t.strip()

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        thinking: bool = False,
    ) -> dict:
        """JSON 스키마를 강제해 dict 로 받는다.

        3단계로 물러난다 — OpenRouter 같은 중계 서비스나 원내 모델은
        스키마 강제를 지원하지 않을 수 있는데, 그때 파이프라인 전체가
        멈추면 안 되기 때문이다:
            1) json_schema + strict   (가장 안전, OpenAI 직결에서 동작)
            2) json_object            (JSON 은 보장, 스키마는 프롬프트로)
            3) 형식 지정 없음          (프롬프트로만 지시)
        한 번 성공한 방식은 기억해서 다음 호출부터 바로 쓴다.
        """
        strict_format = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema, "strict": True},
        }
        schema_hint = (
            "\n\n반드시 아래 JSON 스키마에 맞는 JSON 만 출력하십시오. "
            "설명이나 코드블록 표시 없이 JSON 만 출력하십시오.\n"
            + json.dumps(schema, ensure_ascii=False)
        )

        attempts = [
            ("json_schema", strict_format, system),
            ("json_object", {"type": "json_object"}, system + schema_hint),
            ("none", None, system + schema_hint),
        ]
        if self._json_mode:  # 이미 통하는 방식을 알고 있으면 그것부터
            attempts.sort(key=lambda a: a[0] != self._json_mode)

        last: Exception | None = None
        for mode, fmt, sys_prompt in attempts:
            try:
                text = self._text_of(
                    self._create(
                        system=sys_prompt,
                        user=user,
                        max_tokens=max_tokens,
                        response_format=fmt,
                    )
                )
                result = json.loads(self._strip_fence(text))
            except (LLMError, json.JSONDecodeError) as exc:
                log.debug("JSON 모드 %s 실패: %s", mode, exc)
                last = exc
                continue
            if self._json_mode != mode:
                log.info("JSON 응답 모드: %s", mode)
                self._json_mode = mode
            return result

        raise LLMError(f"JSON 응답을 받지 못했습니다 (마지막 오류: {last})")


# ---------------------------------------------------------------------------
# 테스트 / 프롬프트 확인용
# ---------------------------------------------------------------------------


class DryRunClient:
    """API 를 부르지 않고 프롬프트만 찍어보는 클라이언트.

    --dry-run 으로 파이프라인 배선(로드 → 정규화 → 팬아웃 → 취합)이
    제대로 도는지, 프롬프트에 뭐가 들어가는지 API 키 없이 확인한다.
    """

    def __init__(self, *, echo: bool = False):
        self.echo = echo
        self.calls: list[dict] = []

    def _record(self, kind: str, system: str, user: str) -> None:
        self.calls.append({"kind": kind, "system": system, "user": user})
        if self.echo:
            print(f"\n{'=' * 70}\n[{kind}] SYSTEM\n{'=' * 70}\n{system}")
            print(f"\n{'-' * 70}\n[{kind}] USER\n{'-' * 70}\n{user}")

    def complete_text(
        self, *, system: str, user: str, max_tokens: int, thinking: bool = False
    ) -> str:
        self._record("article", system, user)
        return "(dry-run: 1단계 요약이 여기 들어갑니다)"

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        thinking: bool = False,
    ) -> dict:
        # 스키마 모양으로 어느 에이전트인지 구분한다.
        if "mappings" in schema.get("properties", {}):
            self._record("mapping", system, user)
            return {"mappings": [], "confidence": "low"}
        self._record("law", system, user)
        return {
            "headline": "(dry-run: 한 줄 요약)",
            "body": "(dry-run: 본문 요약)",
            "caveats": [],
        }


def build_client(settings: LLMSettings, *, dry_run: bool = False, echo: bool = False) -> LLMClient:
    """설정에 맞는 클라이언트를 만든다.

    QWEN 전환 시 여기에 분기 한 줄을 추가하면 된다.
    """
    if dry_run:
        return DryRunClient(echo=echo)
    if settings.provider == "openai":
        return OpenAIClient(settings)
    if settings.provider == "anthropic":
        return AnthropicClient(settings)
    raise LLMError(f"알 수 없는 프로바이더: {settings.provider}")
