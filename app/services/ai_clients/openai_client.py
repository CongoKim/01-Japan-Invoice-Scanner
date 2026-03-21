from __future__ import annotations

import asyncio
import base64
import time
from openai import AsyncOpenAI

from app.config import get_effective_api_key, settings
from app.models.invoice import InvoiceFields
from app.services.ai_clients.base import AIClient
from app.services.prompt import EXTRACTION_PROMPT, MULTI_PAGE_DETECTION_PROMPT


class OpenAIClient(AIClient):
    def __init__(self) -> None:
        api_key = get_effective_api_key("openai")
        if not api_key:
            raise ValueError("OpenAI API key is not set. Please configure it in the API Keys panel.")
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = settings.openai_model
        self._rate_limit_lock = asyncio.Lock()
        self._last_request_started_at = 0.0

    def _image_message(self, img_bytes: bytes) -> dict:
        b64 = base64.b64encode(img_bytes).decode()
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        }

    async def _throttle_request(self) -> None:
        interval = max(0.0, settings.openai_min_interval_seconds)
        if interval <= 0:
            return

        async with self._rate_limit_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_started_at
            if self._last_request_started_at and elapsed < interval:
                await asyncio.sleep(interval - elapsed)
            self._last_request_started_at = time.monotonic()

    async def extract_invoice(
        self,
        images: list[bytes],
        file_name: str,
        prompt: str | None = None,
    ) -> InvoiceFields:
        content = []
        for img_bytes in images:
            content.append(self._image_message(img_bytes))
        content.append({"type": "text", "text": prompt or EXTRACTION_PROMPT})

        await self._throttle_request()
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            temperature=0.1,
            max_tokens=settings.openai_extract_max_tokens,
        )

        text = response.choices[0].message.content
        data = self.parse_json_response(text)
        return self.dict_to_invoice(data, file_name, f"openai:{self.model}")

    async def detect_multi_invoice(self, images: list[bytes]) -> dict:
        content = []
        for i, img_bytes in enumerate(images):
            content.append({"type": "text", "text": f"Page {i + 1}:"})
            content.append(self._image_message(img_bytes))
        content.append({"type": "text", "text": MULTI_PAGE_DETECTION_PROMPT})

        await self._throttle_request()
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            temperature=0.1,
            max_tokens=settings.openai_detect_max_tokens,
        )

        text = response.choices[0].message.content
        return self.parse_json_response(text)
