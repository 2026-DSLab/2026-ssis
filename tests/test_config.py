"""설정과 로깅의 보안 관련 회귀 테스트."""

import logging

from lawtrack.config import (
    load_openai_settings,
    load_verification_settings,
    setup_logging,
)


def test_http_client_request_logs_are_suppressed():
    setup_logging("INFO")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_openai_summary_is_enabled_when_key_is_present(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test-model")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_SUMMARY_ENABLED", raising=False)

    settings = load_openai_settings(tmp_path / "missing.env")

    assert settings.configured is True
    assert settings.model == "gpt-test-model"
    assert settings.provider == "openai"
    assert settings.base_url == ""


def test_openrouter_base_url_selects_provider_and_model_default(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1/")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_SUMMARY_ENABLED", raising=False)

    settings = load_openai_settings(tmp_path / "missing.env")

    assert settings.configured is True
    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.provider == "openrouter"
    assert settings.model == "openai/gpt-4o-mini"


def test_verifier_defaults_to_enabled_and_inherits_summary_model(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENAI_MODEL", "openai/writer-model")
    monkeypatch.delenv("OPENAI_SUMMARY_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_VERIFY_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_VERIFY_MODEL", raising=False)

    openai = load_openai_settings(tmp_path / "missing.env")
    verification = load_verification_settings(openai)

    assert verification.enabled is True
    assert verification.required is True
    assert verification.fail_closed is True
    assert verification.model == "openai/writer-model"
