import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import API_PROVIDERS, runtime_api_keys, settings
from app.main import app
from app.routers import settings as settings_router
from app.services import orchestrator


class GeminiValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_validate_gemini_uses_async_list_pager(self):
        pager = SimpleNamespace(page=[SimpleNamespace(name="gemini-test")])
        list_models = AsyncMock(return_value=pager)
        fake_client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(list=list_models)
            )
        )

        with patch("google.genai.Client", return_value=fake_client):
            ok, error = await settings_router._validate_gemini("gemini-key")

        self.assertTrue(ok)
        self.assertEqual(error, "")
        list_models.assert_awaited_once_with(config={"page_size": 1})

    async def test_validate_gemini_timeout_returns_readable_error(self):
        list_models = AsyncMock(side_effect=asyncio.TimeoutError())
        fake_client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(list=list_models)
            )
        )

        with patch("google.genai.Client", return_value=fake_client):
            ok, error = await settings_router._validate_gemini("gemini-key")

        self.assertFalse(ok)
        self.assertEqual(error, "验证超时，请稍后重试。")

    async def test_validate_gemini_auth_returns_readable_error(self):
        list_models = AsyncMock(side_effect=Exception("401 Unauthorized"))
        fake_client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(list=list_models)
            )
        )

        with patch("google.genai.Client", return_value=fake_client):
            ok, error = await settings_router._validate_gemini("gemini-key")

        self.assertFalse(ok)
        self.assertEqual(error, "鉴权失败，请检查 API 密钥是否正确。")

    async def test_validate_gemini_network_returns_readable_error(self):
        list_models = AsyncMock(side_effect=OSError("Connection refused"))
        fake_client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(list=list_models)
            )
        )

        with patch("google.genai.Client", return_value=fake_client):
            ok, error = await settings_router._validate_gemini("gemini-key")

        self.assertFalse(ok)
        self.assertEqual(
            error,
            "验证 API 密钥时发生网络错误，请稍后重试。",
        )


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

        self._env_keys = dict(runtime_api_keys._env_keys)
        self._browser_overrides = dict(runtime_api_keys._browser_overrides)
        self._validation = dict(settings_router._validation)
        self._validation_errors = dict(settings_router._validation_errors)
        self._validation_fingerprints = dict(settings_router._validation_fingerprints)
        self.addCleanup(self._restore_state)

        self._reset_state()

    def _reset_state(self):
        for provider in API_PROVIDERS:
            runtime_api_keys._env_keys[provider] = ""
            runtime_api_keys._browser_overrides[provider] = None
            settings_router._validation[provider] = None
            settings_router._validation_errors[provider] = ""
            settings_router._validation_fingerprints[provider] = None
        orchestrator.reset_clients()

    def _restore_state(self):
        runtime_api_keys._env_keys.update(self._env_keys)
        runtime_api_keys._browser_overrides.update(self._browser_overrides)
        settings_router._validation.update(self._validation)
        settings_router._validation_errors.update(self._validation_errors)
        settings_router._validation_fingerprints.update(self._validation_fingerprints)
        orchestrator.reset_clients()

    def test_get_keys_reports_browser_source_for_override(self):
        response = self.client.post(
            "/api/settings/keys",
            json={
                "gemini_api_key": "browser-gemini",
                "openai_api_key": "",
                "anthropic_api_key": "",
            },
        )
        self.assertEqual(response.status_code, 200)

        payload = self.client.get("/api/settings/keys").json()
        self.assertTrue(payload["gemini"]["present"])
        self.assertEqual(payload["gemini"]["source"], "browser")
        self.assertIsNone(payload["gemini"]["valid"])

    def test_empty_browser_value_falls_back_to_env(self):
        runtime_api_keys._env_keys["gemini"] = "env-gemini"

        self.client.post(
            "/api/settings/keys",
            json={
                "gemini_api_key": "browser-gemini",
                "openai_api_key": "",
                "anthropic_api_key": "",
            },
        )

        response = self.client.post(
            "/api/settings/keys",
            json={
                "gemini_api_key": "",
                "openai_api_key": "",
                "anthropic_api_key": "",
            },
        )
        self.assertEqual(response.status_code, 200)

        payload = self.client.get("/api/settings/keys").json()
        self.assertTrue(payload["gemini"]["present"])
        self.assertEqual(payload["gemini"]["source"], "env")
        self.assertIsNone(payload["gemini"]["valid"])

    def test_changed_effective_key_resets_only_that_provider_validation(self):
        self.client.post(
            "/api/settings/keys",
            json={
                "gemini_api_key": "gemini-a",
                "openai_api_key": "openai-a",
                "anthropic_api_key": "anthropic-a",
            },
        )

        with patch.object(settings_router, "_validate_gemini", new=AsyncMock(return_value=(True, ""))), \
                patch.object(settings_router, "_validate_openai", new=AsyncMock(return_value=(True, ""))), \
                patch.object(settings_router, "_validate_anthropic", new=AsyncMock(return_value=(True, ""))):
            validate_response = self.client.post("/api/settings/validate")

        self.assertEqual(validate_response.status_code, 200)
        before = self.client.get("/api/settings/keys").json()
        self.assertTrue(before["gemini"]["valid"])
        self.assertTrue(before["openai"]["valid"])
        self.assertTrue(before["anthropic"]["valid"])

        self.client.post(
            "/api/settings/keys",
            json={
                "gemini_api_key": "gemini-b",
                "openai_api_key": "openai-a",
                "anthropic_api_key": "anthropic-a",
            },
        )

        after = self.client.get("/api/settings/keys").json()
        self.assertIsNone(after["gemini"]["valid"])
        self.assertEqual(after["gemini"]["error"], "")
        self.assertTrue(after["openai"]["valid"])
        self.assertTrue(after["anthropic"]["valid"])

    def test_get_keys_includes_runtime_model_versions(self):
        payload = self.client.get("/api/settings/keys").json()

        self.assertEqual(
            payload["_models"],
            {
                "gemini": settings.gemini_model,
                "openai": settings.openai_model,
                "anthropic": settings.claude_model,
            },
        )

    def test_validate_keys_returns_independent_provider_results(self):
        self.client.post(
            "/api/settings/keys",
            json={
                "gemini_api_key": "gemini-a",
                "openai_api_key": "openai-a",
                "anthropic_api_key": "anthropic-a",
            },
        )

        with patch.object(
            settings_router,
            "_validate_gemini",
            new=AsyncMock(return_value=(False, "鉴权失败，请检查 API 密钥是否正确。")),
        ), patch.object(
            settings_router,
            "_validate_openai",
            new=AsyncMock(return_value=(True, "")),
        ), patch.object(
            settings_router,
            "_validate_anthropic",
            new=AsyncMock(return_value=(True, "")),
        ):
            response = self.client.post("/api/settings/validate")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["gemini"]["ok"])
        self.assertEqual(
            payload["gemini"]["error"],
            "鉴权失败，请检查 API 密钥是否正确。",
        )
        self.assertTrue(payload["openai"]["ok"])
        self.assertTrue(payload["anthropic"]["ok"])


if __name__ == "__main__":
    unittest.main()
