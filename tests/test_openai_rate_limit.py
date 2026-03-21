import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.services.ai_clients.openai_client import OpenAIClient
from app.services.orchestrator import _extract_retry_delay_seconds


class OpenAIRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._extract_max_tokens = settings.openai_extract_max_tokens
        self._detect_max_tokens = settings.openai_detect_max_tokens
        self._min_interval_seconds = settings.openai_min_interval_seconds
        self.addCleanup(self._restore_settings)

    def _restore_settings(self):
        settings.openai_extract_max_tokens = self._extract_max_tokens
        settings.openai_detect_max_tokens = self._detect_max_tokens
        settings.openai_min_interval_seconds = self._min_interval_seconds

    async def test_extract_invoice_uses_reduced_max_tokens(self):
        settings.openai_extract_max_tokens = 1234
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
            )
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with patch(
            "app.services.ai_clients.openai_client.get_effective_api_key",
            return_value="openai-key",
        ), patch(
            "app.services.ai_clients.openai_client.AsyncOpenAI",
            return_value=fake_client,
        ):
            client = OpenAIClient()
            with patch.object(client, "_throttle_request", new=AsyncMock()):
                await client.extract_invoice([b"fake-image"], "invoice.jpg")

        self.assertEqual(create.await_args.kwargs["max_tokens"], 1234)

    async def test_detect_multi_invoice_uses_reduced_max_tokens(self):
        settings.openai_detect_max_tokens = 321
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
            )
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with patch(
            "app.services.ai_clients.openai_client.get_effective_api_key",
            return_value="openai-key",
        ), patch(
            "app.services.ai_clients.openai_client.AsyncOpenAI",
            return_value=fake_client,
        ):
            client = OpenAIClient()
            with patch.object(client, "_throttle_request", new=AsyncMock()):
                await client.detect_multi_invoice([b"page-1"])

        self.assertEqual(create.await_args.kwargs["max_tokens"], 321)

    async def test_throttle_waits_for_min_interval(self):
        settings.openai_min_interval_seconds = 6.0
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock()))
        )

        with patch(
            "app.services.ai_clients.openai_client.get_effective_api_key",
            return_value="openai-key",
        ), patch(
            "app.services.ai_clients.openai_client.AsyncOpenAI",
            return_value=fake_client,
        ):
            client = OpenAIClient()

        client._last_request_started_at = 12.0
        sleep_mock = AsyncMock()
        with patch(
            "app.services.ai_clients.openai_client.time.monotonic",
            side_effect=[16.0, 18.5],
        ), patch(
            "app.services.ai_clients.openai_client.asyncio.sleep",
            new=sleep_mock,
        ):
            await client._throttle_request()

        sleep_mock.assert_awaited_once_with(2.0)


class RetryDelayParsingTests(unittest.TestCase):
    def test_extract_retry_delay_seconds_parses_seconds(self):
        exc = Exception("Rate limit reached. Please try again in 6.608s.")
        self.assertEqual(_extract_retry_delay_seconds(exc), 6.608)

    def test_extract_retry_delay_seconds_parses_milliseconds(self):
        exc = Exception("Rate limit reached. Please try again in 487ms.")
        self.assertEqual(_extract_retry_delay_seconds(exc), 0.487)

    def test_extract_retry_delay_seconds_returns_none_without_hint(self):
        exc = Exception("Something else failed")
        self.assertIsNone(_extract_retry_delay_seconds(exc))
