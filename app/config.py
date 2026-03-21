from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings

ApiProvider = Literal["gemini", "openai", "anthropic"]
ApiKeySource = Literal["browser", "env", "none"]
API_PROVIDERS: tuple[ApiProvider, ...] = ("gemini", "openai", "anthropic")


class Settings(BaseSettings):
    gemini_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_model: str = "gemini-2.5-pro"
    openai_model: str = "gpt-4o"
    claude_model: str = "claude-sonnet-4-6-20250514"
    openai_extract_max_tokens: int = 1200
    openai_detect_max_tokens: int = 512
    openai_min_interval_seconds: float = 6.0
    max_concurrency: int = 10
    port: int = 8000
    max_upload_size_bytes: int = 100 * 1024 * 1024
    task_retention_seconds: int = 60 * 60
    cleanup_interval_seconds: int = 10 * 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


class RuntimeApiKeyStore:
    """Resolve browser overrides against the startup-time .env snapshot."""

    def __init__(self, base_settings: Settings) -> None:
        self._env_keys: dict[ApiProvider, str] = {
            "gemini": base_settings.gemini_api_key,
            "openai": base_settings.openai_api_key,
            "anthropic": base_settings.anthropic_api_key,
        }
        self._browser_overrides: dict[ApiProvider, str | None] = {
            provider: None for provider in API_PROVIDERS
        }

    def sync_browser_keys(self, keys: dict[ApiProvider, str]) -> dict[ApiProvider, bool]:
        changed: dict[ApiProvider, bool] = {}
        for provider in API_PROVIDERS:
            normalized = keys.get(provider, "").strip() or None
            changed[provider] = self._browser_overrides[provider] != normalized
            self._browser_overrides[provider] = normalized
        return changed

    def get_env_key(self, provider: ApiProvider) -> str:
        return self._env_keys[provider]

    def get_browser_override(self, provider: ApiProvider) -> str | None:
        return self._browser_overrides[provider]

    def get_effective_key(self, provider: ApiProvider) -> str:
        return self._browser_overrides[provider] or self._env_keys[provider]

    def get_source(self, provider: ApiProvider) -> ApiKeySource:
        if self._browser_overrides[provider]:
            return "browser"
        if self._env_keys[provider]:
            return "env"
        return "none"


settings = Settings()
runtime_api_keys = RuntimeApiKeyStore(settings)


def get_effective_api_key(provider: ApiProvider) -> str:
    return runtime_api_keys.get_effective_key(provider)


def get_api_key_source(provider: ApiProvider) -> ApiKeySource:
    return runtime_api_keys.get_source(provider)


def has_effective_api_key(provider: ApiProvider) -> bool:
    return bool(get_effective_api_key(provider))
