from __future__ import annotations

import base64
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

    def _image_message(self, img_bytes: bytes) -> dict:
        b64 = base64.b64encode(img_bytes).decode()
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        }

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

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            temperature=0.1,
            max_tokens=4096,
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

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            temperature=0.1,
            max_tokens=1024,
        )

        text = response.choices[0].message.content
        return self.parse_json_response(text)
