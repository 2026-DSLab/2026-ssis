"""summarizer/config.py의 OpenRouter 지원 테스트.

OpenRouter는 OpenAI 호환 API를 그대로 쓰되 주소(base_url)와 모델명 형식
(provider/model)만 다름 — load_settings()가 이 차이를 사용자가 매번
SUMMARY_BASE_URL을 적지 않아도 되게 자동으로 메워줌.
"""

from __future__ import annotations

import pytest

from summarizer.config import ConfigError, OPENROUTER_BASE_URL, load_settings
from summarizer.llm import OpenAIClient, build_client


@pytest.fixture
def no_dotenv_file(tmp_path):
    """존재하지 않는 .env 경로 — 실제 프로젝트 .env를 건드리지 않고
    monkeypatch로 넣은 OS 환경변수만으로 load_settings를 테스트함."""
    return tmp_path / "does_not_exist.env"


def test_openrouter_without_model_is_rejected(monkeypatch, no_dotenv_file):
    """OpenRouter 모델명은 'provider/model' 형식이라 OpenAI 전용
    기본값(gpt-5.4-mini)을 그대로 쓰면 100% 실패함 — 조용히 틀린
    기본값을 쓰는 대신 명시를 요구해야 함."""
    monkeypatch.setenv("SUMMARY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("SUMMARY_MODEL", raising=False)

    with pytest.raises(ConfigError, match="SUMMARY_MODEL"):
        load_settings(no_dotenv_file)


def test_openrouter_base_url_auto_filled(monkeypatch, no_dotenv_file):
    """SUMMARY_BASE_URL을 따로 안 적어도 OpenRouter 주소가 자동으로 들어가야 함."""
    monkeypatch.setenv("SUMMARY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("SUMMARY_MODEL", "openai/gpt-4o-mini")
    monkeypatch.delenv("SUMMARY_BASE_URL", raising=False)

    settings = load_settings(no_dotenv_file)

    assert settings.llm.base_url == OPENROUTER_BASE_URL
    assert settings.llm.model == "openai/gpt-4o-mini"
    assert settings.llm.api_key == "sk-or-test"


def test_explicit_base_url_overrides_openrouter_default(monkeypatch, no_dotenv_file):
    """SUMMARY_BASE_URL을 명시했으면(사내 프록시 등) 그 값이 우선함."""
    monkeypatch.setenv("SUMMARY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("SUMMARY_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("SUMMARY_BASE_URL", "https://internal-proxy.example/v1")

    settings = load_settings(no_dotenv_file)

    assert settings.llm.base_url == "https://internal-proxy.example/v1"


def test_openai_provider_unaffected(monkeypatch, no_dotenv_file):
    """기본 openai 프로바이더는 base_url이 여전히 None(직결)이어야 함."""
    monkeypatch.setenv("SUMMARY_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("SUMMARY_BASE_URL", raising=False)

    settings = load_settings(no_dotenv_file)

    assert settings.llm.base_url is None


def test_build_client_routes_openrouter_to_openai_client(monkeypatch, no_dotenv_file):
    """openrouter는 별도 구현체가 없음 — OpenAIClient가 그대로 처리해야 함."""
    monkeypatch.setenv("SUMMARY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("SUMMARY_MODEL", "openai/gpt-4o-mini")

    settings = load_settings(no_dotenv_file)
    client = build_client(settings.llm)

    assert isinstance(client, OpenAIClient)
