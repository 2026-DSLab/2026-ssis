"""요약 파이프라인 설정.

lawtrack/config.py 와 같은 방식 — 모든 설정은 환경변수(.env)에서 읽는다.
대화형 입력을 쓰지 않는 이유도 같다: 배치 실행에서 멈추면 안 된다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv 미설치 시에도 OS 환경변수로 동작
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.4-mini"

#: 프로바이더별 API 키 환경변수. QWEN 을 붙일 때 여기에 한 줄 추가한다.
API_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

#: OpenRouter 는 OpenAI 호환 API 를 그대로 쓰되 주소만 다르다
#: (llm.py OpenAIClient 가 그대로 처리한다). SUMMARY_BASE_URL 을 매번
#: 손으로 적지 않아도 되도록 기본값을 여기 박아 둔다 — 더 구체적인 값이
#: 필요하면 SUMMARY_BASE_URL 로 덮어쓸 수 있다.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class ConfigError(RuntimeError):
    """필수 설정 누락."""


@dataclass(frozen=True)
class LLMSettings:
    """LLM 호출 설정."""

    api_key: str
    provider: str = DEFAULT_PROVIDER
    """openai | anthropic | openrouter. 원내 QWEN 전환 시 여기에 값을 추가한다.

    openrouter 는 별도 구현체가 없다 — OpenAI 호환 API 를 그대로 쓰므로
    llm.py 의 OpenAIClient 가 처리하고, base_url 만 OpenRouter 주소로
    바뀐다(load_settings 가 자동으로 채운다)."""

    model: str = DEFAULT_MODEL

    base_url: str | None = None
    """API 주소. None 이면 프로바이더 기본값(OpenAI 는 api.openai.com).

    OpenRouter 처럼 OpenAI 호환 API 를 쓰는 중계 서비스는 여기를 바꾼다:
        SUMMARY_BASE_URL=https://openrouter.ai/api/v1
    원내 QWEN 이 OpenAI 호환 엔드포인트를 제공한다면 이것만 바꿔도 된다."""

    article_max_tokens: int = 2048
    """1단계는 조문 하나당 2~3문장이라 낮게 잡아도 안전하다."""

    law_max_tokens: int = 8192
    """2단계는 조문 개수만큼 길어질 수 있어 여유를 둔다."""

    mapping_max_tokens: int = 4096
    """매핑 에이전트 — 조문 항목 수만큼 대응 관계를 내야 해서 중간 크기."""

    mapping_thinking: bool = True
    """구↔신 대응 판정은 조문 전체를 놓고 따져야 하는 추론 작업이라 기본 on."""

    enable_verifier: bool = True
    """감수(사실 대조) 사용 여부. summarizer/verifier.py의 코드 기반
    verify_summaries()를 켤지 말지만 결정한다 — LLM을 호출하지 않으므로
    비용과는 무관하고(끈다고 절약되는 토큰 없음), 순수 검증 단계 자체를
    건너뛰고 싶을 때만 끈다."""

    article_thinking: bool = False
    """1단계는 '주어진 두 문장을 다듬는' 단순 작업이라 기본 off.
    조문이 복잡해 요약 품질이 떨어지면 True 로 올려볼 것."""

    law_thinking: bool = True
    """2단계는 여러 조문을 묶어 개정 취지와 연결하는 종합 작업이라 기본 on.
    adaptive thinking — 모델이 필요한 만큼만 생각한다."""

    effort: str | None = None
    """low | medium | high | xhigh | max. None 이면 API 기본값(high).
    비용이 문제면 medium 부터 내려볼 것."""

    timeout: float = 600.0
    max_retries: int = 3
    """429/5xx 는 SDK 가 알아서 재시도한다. 여기서는 횟수만 조정."""


@dataclass(frozen=True)
class PipelineSettings:
    """오케스트레이션 설정."""

    max_workers: int = 4
    """1단계 조문 팬아웃 동시성. 조문끼리 독립이라 병렬이 안전하다.
    rate limit 에 걸리면 낮출 것."""

    output_dir: Path = field(default=PROJECT_ROOT / "out" / "summaries")


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    pipeline: PipelineSettings
    log_level: str = "INFO"


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"환경변수 {key} 는 정수여야 합니다: {raw!r}") from exc


def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def load_settings(env_file: str | Path | None = None, *, require_api_key: bool = True) -> Settings:
    """환경변수에서 설정을 읽는다.

    우선순위: 이미 설정된 OS 환경변수 > .env 파일

    require_api_key=False 는 --dry-run 용 — API 키 없이 프롬프트만
    확인하고 싶을 때 쓴다.
    """
    path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if path.exists():
        load_dotenv(path, override=False)

    provider = (os.environ.get("SUMMARY_PROVIDER", "").strip() or DEFAULT_PROVIDER).lower()
    if provider not in API_KEY_ENV:
        raise ConfigError(
            f"SUMMARY_PROVIDER 는 {' | '.join(API_KEY_ENV)} 중 하나여야 합니다: {provider!r}"
        )

    key_env = API_KEY_ENV[provider]
    api_key = os.environ.get(key_env, "").strip()
    if require_api_key and not api_key:
        raise ConfigError(
            f"환경변수 {key_env} 가 설정되지 않았습니다. "
            f"{path} 에 {key_env}=... 를 추가하세요."
        )

    effort = os.environ.get("SUMMARY_EFFORT", "").strip() or None
    if effort and effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError(
            f"SUMMARY_EFFORT 는 low|medium|high|xhigh|max 중 하나여야 합니다: {effort!r}"
        )

    model = os.environ.get("SUMMARY_MODEL", "").strip()
    if provider == "openrouter" and not model:
        # OpenRouter 모델명은 "provider/model" 형식이라(예: openai/gpt-4o-mini,
        # anthropic/claude-3.5-sonnet) OpenAI 전용 기본값(gpt-5.4-mini)을
        # 그대로 쓰면 100% 실패한다. 조용히 틀린 기본값을 쓰는 대신 명시를
        # 요구한다 — https://openrouter.ai/models 에서 실제 이름을 확인할 것.
        raise ConfigError(
            "SUMMARY_PROVIDER=openrouter 는 SUMMARY_MODEL 을 반드시 명시해야 합니다"
            " (형식: provider/model, 예: openai/gpt-4o-mini). "
            "https://openrouter.ai/models 에서 확인하세요."
        )
    if not model:
        model = DEFAULT_MODEL

    base_url = os.environ.get("SUMMARY_BASE_URL", "").strip() or None
    if provider == "openrouter" and not base_url:
        base_url = OPENROUTER_BASE_URL

    llm = LLMSettings(
        api_key=api_key,
        provider=provider,
        model=model,
        base_url=base_url,
        article_max_tokens=_int("SUMMARY_ARTICLE_MAX_TOKENS", 2048),
        law_max_tokens=_int("SUMMARY_LAW_MAX_TOKENS", 8192),
        article_thinking=_bool("SUMMARY_ARTICLE_THINKING", False),
        law_thinking=_bool("SUMMARY_LAW_THINKING", True),
        mapping_max_tokens=_int("SUMMARY_MAPPING_MAX_TOKENS", 4096),
        mapping_thinking=_bool("SUMMARY_MAPPING_THINKING", True),
        enable_verifier=_bool("SUMMARY_ENABLE_VERIFIER", True),
        effort=effort,
        timeout=float(os.environ.get("SUMMARY_TIMEOUT", "600") or 600),
        max_retries=_int("SUMMARY_MAX_RETRIES", 3),
    )

    pipeline = PipelineSettings(
        max_workers=_int("SUMMARY_MAX_WORKERS", 4),
        output_dir=Path(
            os.environ.get("SUMMARY_OUTPUT_DIR", str(PROJECT_ROOT / "out" / "summaries"))
        ),
    )

    return Settings(
        llm=llm,
        pipeline=pipeline,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
    )
