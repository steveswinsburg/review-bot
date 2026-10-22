"""Unit tests for the AI provider layer in review_bot.py."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_bot as rb  # noqa: E402


def test_load_ollama_provider_from_bundled_config(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    provider = rb.load_ai_provider("ollama")
    assert provider.api_style == "ollama"
    assert provider.base_url == "http://localhost:11434"
    assert provider.model == "llama3"
    assert provider.max_payload_chars == 30_000


def test_ollama_legacy_env_overrides(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://other:1234")
    monkeypatch.setenv("OLLAMA_MODEL", "mistral")
    provider = rb.load_ai_provider("ollama")
    assert provider.base_url == "http://other:1234"
    assert provider.model == "mistral"


def test_ollama_request_and_response_shaping():
    provider = rb.AIProvider("ollama", {
        "api_style": "ollama",
        "base_url": "http://localhost:11434",
        "model": "llama3",
    })
    url, headers, payload = provider._build_request("hello")
    assert url == "http://localhost:11434/api/generate"
    assert payload == {"model": "llama3", "prompt": "hello", "stream": False}
    assert "Authorization" not in headers
    assert provider._parse_response({"response": " hi "}) == "hi"


def test_openai_style_provider(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    provider = rb.load_ai_provider("cursor")
    assert provider.api_style == "openai"
    url, headers, payload = provider._build_request("hello")
    assert url == "https://api.cursor.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer " + "test-key"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["model"] == provider.model
    response = {"choices": [{"message": {"content": " ok "}}]}
    assert provider._parse_response(response) == "ok"
    assert provider._parse_response({"choices": []}) == ""


def test_anthropic_style_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-test")
    provider = rb.load_ai_provider("anthropic")
    assert provider.api_style == "anthropic"
    url, headers, payload = provider._build_request("hello")
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "ak-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    response = {"content": [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]}
    assert provider._parse_response(response) == "a\nb"


def test_remote_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(rb.AIProviderError, match="OPENAI_API_KEY"):
        rb.load_ai_provider("openai")


def test_unknown_provider_raises():
    with pytest.raises(rb.AIProviderError, match="No config file"):
        rb.load_ai_provider("does-not-exist")


def test_invalid_api_style_raises():
    with pytest.raises(rb.AIProviderError, match="unsupported api_style"):
        rb.AIProvider("bad", {
            "api_style": "bogus",
            "base_url": "http://x",
            "model": "m",
        })


def test_missing_base_url_or_model_raises():
    with pytest.raises(rb.AIProviderError, match="required"):
        rb.AIProvider("bad", {"api_style": "ollama", "base_url": "", "model": ""})


def test_bundled_provider_configs_are_valid_json():
    providers_dir = Path(__file__).resolve().parent.parent / "providers"
    config_files = sorted(providers_dir.glob("*.json"))
    assert config_files, "expected bundled provider configs"
    for config_file in config_files:
        config = json.loads(config_file.read_text(encoding="utf-8"))
        assert config["api_style"] in rb.AIProvider.SUPPORTED_API_STYLES
        assert config["base_url"]
        assert config["model"]
        if config["api_style"] != "ollama":
            assert config["api_key_env"], (
                f"{config_file.name}: remote providers must name an API key env var"
            )
            # Secrets must never be stored in config files.
            assert "api_key" not in config
