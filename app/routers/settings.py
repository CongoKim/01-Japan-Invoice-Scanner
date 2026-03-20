import asyncio
import inspect
from collections.abc import Awaitable, Callable

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from app.config import API_PROVIDERS, ApiProvider, get_api_key_source, get_effective_api_key, runtime_api_keys
from app.services import orchestrator

router = APIRouter()

_MISSING_KEY_FINGERPRINT = "<<missing>>"
_validation: dict[ApiProvider, bool | None] = {
    provider: None for provider in API_PROVIDERS
}
_validation_errors: dict[ApiProvider, str] = {
    provider: "" for provider in API_PROVIDERS
}
_validation_fingerprints: dict[ApiProvider, str | None] = {
    provider: None for provider in API_PROVIDERS
}


class ApiKeys(BaseModel):
    gemini_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""


def _current_fingerprint(provider: ApiProvider) -> str:
    return get_effective_api_key(provider) or _MISSING_KEY_FINGERPRINT


def _reset_validation(provider: ApiProvider) -> None:
    _validation[provider] = None
    _validation_errors[provider] = ""
    _validation_fingerprints[provider] = None


def _store_validation_result(provider: ApiProvider, ok: bool, error: str) -> None:
    _validation[provider] = ok
    _validation_errors[provider] = error
    _validation_fingerprints[provider] = _current_fingerprint(provider)


def _get_current_validation(provider: ApiProvider) -> tuple[bool | None, str]:
    if _validation_fingerprints[provider] != _current_fingerprint(provider):
        return None, ""
    valid = _validation[provider]
    if valid is None:
        return None, ""
    return valid, _validation_errors[provider]


def _extract_message(exc: Exception) -> str:
    message = str(exc)
    for line in message.splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return message[:160]


def _humanize_validation_error(exc: Exception) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return "验证超时，请稍后重试。"

    if isinstance(exc, (httpx.NetworkError, OSError)):
        return "验证 API 密钥时发生网络错误，请稍后重试。"

    message = _extract_message(exc)
    lowered = message.lower()
    exc_name = exc.__class__.__name__.lower()

    auth_tokens = (
        "401",
        "403",
        "invalid api key",
        "incorrect api key",
        "authentication",
        "unauthorized",
        "forbidden",
        "permission",
        "access denied",
    )
    network_tokens = (
        "connection",
        "network",
        "dns",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "service unavailable",
        "connection refused",
        "connection reset",
        "ssl",
        "502",
        "503",
        "504",
    )

    if any(token in exc_name for token in ("authentication", "permission", "forbidden")):
        return "鉴权失败，请检查 API 密钥是否正确。"
    if any(token in exc_name for token in ("network", "connection", "timeout")):
        return "验证 API 密钥时发生网络错误，请稍后重试。"
    if any(token in lowered for token in auth_tokens):
        return "鉴权失败，请检查 API 密钥是否正确。"
    if any(token in lowered for token in network_tokens):
        return "验证 API 密钥时发生网络错误，请稍后重试。"

    if message:
        return f"验证失败：{message}"
    return "验证失败，请稍后重试。"


async def _validate_gemini(key: str) -> tuple[bool, str]:
    try:
        from google import genai

        client = genai.Client(api_key=key)
        listing = client.aio.models.list(config={"page_size": 1})
        if hasattr(listing, "__aiter__"):
            async def consume_first_item() -> None:
                async for _ in listing:
                    break

            await asyncio.wait_for(consume_first_item(), timeout=10)
        elif inspect.isawaitable(listing):
            pager = await asyncio.wait_for(listing, timeout=10)
            _ = getattr(pager, "page", None)
        else:
            raise TypeError("Unexpected Gemini models.list() result type")
        return True, ""
    except Exception as exc:
        return False, _humanize_validation_error(exc)


async def _validate_openai(key: str) -> tuple[bool, str]:
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=key)
        await asyncio.wait_for(client.models.list(), timeout=10)
        return True, ""
    except Exception as exc:
        return False, _humanize_validation_error(exc)


async def _validate_anthropic(key: str) -> tuple[bool, str]:
    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=key)
        await asyncio.wait_for(client.models.list(), timeout=10)
        return True, ""
    except Exception as exc:
        return False, _humanize_validation_error(exc)


def _status_payload(provider: ApiProvider) -> dict[str, bool | str | None]:
    valid, error = _get_current_validation(provider)
    return {
        "present": bool(get_effective_api_key(provider)),
        "source": get_api_key_source(provider),
        "valid": valid,
        "error": error,
    }


@router.post("/api/settings/keys")
async def update_api_keys(keys: ApiKeys):
    previous_effective_keys = {
        provider: get_effective_api_key(provider) for provider in API_PROVIDERS
    }

    runtime_api_keys.sync_browser_keys(
        {
            "gemini": keys.gemini_api_key,
            "openai": keys.openai_api_key,
            "anthropic": keys.anthropic_api_key,
        }
    )

    for provider in API_PROVIDERS:
        if get_effective_api_key(provider) != previous_effective_keys[provider]:
            _reset_validation(provider)

    orchestrator.reset_clients()
    return {"ok": True}


@router.post("/api/settings/validate")
async def validate_keys():
    validators: dict[ApiProvider, Callable[[str], Awaitable[tuple[bool, str]]]] = {
        "gemini": _validate_gemini,
        "openai": _validate_openai,
        "anthropic": _validate_anthropic,
    }

    validation_tasks = {
        provider: asyncio.create_task(validators[provider](get_effective_api_key(provider)))
        for provider in API_PROVIDERS
        if get_effective_api_key(provider)
    }

    gathered: dict[ApiProvider, tuple[bool, str] | Exception] = {}
    if validation_tasks:
        results = await asyncio.gather(*validation_tasks.values(), return_exceptions=True)
        gathered = dict(zip(validation_tasks.keys(), results))

    payload: dict[ApiProvider, dict[str, bool | str]] = {}
    for provider in API_PROVIDERS:
        if not get_effective_api_key(provider):
            ok = False
            error = "未设置 API 密钥"
        else:
            outcome = gathered[provider]
            if isinstance(outcome, Exception):
                ok = False
                error = _humanize_validation_error(outcome)
            else:
                ok, error = outcome

        _store_validation_result(provider, ok, error)
        payload[provider] = {"ok": ok, "error": error}

    return payload


@router.get("/api/settings/keys")
async def get_api_keys():
    return {
        provider: _status_payload(provider)
        for provider in API_PROVIDERS
    }
