"""Configurable AI backends for digest and review calls."""

import json
import os
import urllib.request
from pathlib import Path

from lib.config import AI_TIMEOUT_SECONDS, MAX_PAYLOAD_CHARS

DEFAULT_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "providers"


class AIProviderError(Exception):
    """Raised when a provider config is invalid or a provider call fails."""


class AIProvider:
    """An AI backend described by a per-provider JSON config file.

    Config schema (providers/<name>.json):
        {
          "api_style":   "ollama" | "openai" | "anthropic",   (required)
          "base_url":    "https://...",                       (required)
          "model":       "model-name",                        (required)
          "api_key_env": "ENV_VAR_HOLDING_KEY",               (required for
                                                               remote styles)
          "max_payload_chars": 30000,                         (optional)
          "timeout_seconds":   600,                           (optional)
          "max_tokens":        4096,                          (optional,
                                                               openai/anthropic)
          "extra_headers": {"Header-Name": "value"}           (optional)
        }

    API keys are never stored in config files; "api_key_env" names the
    environment variable that holds the secret.
    """

    SUPPORTED_API_STYLES = ("ollama", "openai", "anthropic")

    def __init__(self, name: str, config: dict):
        self.name = name
        self.api_style = config.get("api_style", "")
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        self.model = config.get("model", "")
        self.max_payload_chars = int(
            config.get("max_payload_chars", MAX_PAYLOAD_CHARS)
        )
        self.timeout_seconds = int(
            config.get("timeout_seconds", AI_TIMEOUT_SECONDS)
        )
        self.max_tokens = int(config.get("max_tokens", 4096))
        self.extra_headers = dict(config.get("extra_headers", {}))

        if self.api_style not in self.SUPPORTED_API_STYLES:
            raise AIProviderError(
                f"Provider '{name}': unsupported api_style "
                f"'{self.api_style}' (expected one of "
                f"{', '.join(self.SUPPORTED_API_STYLES)})."
            )
        if not self.base_url or not self.model:
            raise AIProviderError(
                f"Provider '{name}': 'base_url' and 'model' are required."
            )

        self.api_key = ""
        api_key_env = config.get("api_key_env", "")
        if api_key_env:
            self.api_key = os.environ.get(api_key_env, "")
        if self.api_style != "ollama" and not self.api_key:
            raise AIProviderError(
                f"Provider '{name}': remote api_style '{self.api_style}' "
                f"requires an API key; set the '{api_key_env or 'api_key_env'}'"
                " environment variable."
            )

    def _build_request(self, prompt: str):
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_style == "ollama":
            url = f"{self.base_url}/api/generate"
            payload = {"model": self.model, "prompt": prompt, "stream": False}
        elif self.api_style == "openai":
            url = f"{self.base_url}/chat/completions"
            headers["Authorization"] = "Bearer " + self.api_key
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.max_tokens,
                "stream": False,
            }
        else:  # anthropic
            url = f"{self.base_url}/v1/messages"
            headers["x-api-key"] = self.api_key
            headers.setdefault("anthropic-version", "2023-06-01")
            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
        return url, headers, payload

    def _parse_response(self, data: dict) -> str:
        if self.api_style == "ollama":
            return (data.get("response") or "").strip()
        if self.api_style == "openai":
            choices = data.get("choices") or []
            if choices:
                message = choices[0].get("message") or {}
                return (message.get("content") or "").strip()
            return ""
        parts = data.get("content") or []
        return "\n".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()

    def request_review(self, prompt: str) -> str:
        """Send the prompt to the configured AI backend and return the text."""
        url, headers, payload = self._build_request(prompt)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=self.timeout_seconds
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return self._parse_response(data)


def load_ai_provider(name: str, config_dir: Path = None) -> AIProvider:
    """Load the provider config file providers/<name>.json.

    The built-in 'ollama' provider works without a config file and honors the
    legacy OLLAMA_URL / OLLAMA_MODEL environment overrides.
    """
    directory = config_dir if config_dir is not None else DEFAULT_PROVIDER_DIR
    config_path = directory / f"{name}.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AIProviderError(
                f"Could not parse provider config {config_path}: {exc}"
            ) from exc
        if name == "ollama":
            if os.environ.get("OLLAMA_URL"):
                config["base_url"] = os.environ["OLLAMA_URL"]
            if os.environ.get("OLLAMA_MODEL"):
                config["model"] = os.environ["OLLAMA_MODEL"]
        return AIProvider(name, config)

    if name == "ollama":
        return AIProvider(name, {
            "api_style": "ollama",
            "base_url": os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            "model": os.environ.get("OLLAMA_MODEL", "llama3"),
        })

    raise AIProviderError(
        f"No config file found for AI provider '{name}' "
        f"(expected {config_path})."
    )
