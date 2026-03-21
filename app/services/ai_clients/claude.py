from __future__ import annotations

import base64

import anthropic

from app.config import get_effective_api_key, settings
from app.models.invoice import InvoiceFields
from app.services.ai_clients.base import AIClient
from app.services.prompt import (
    EXTRACTION_PROMPT,
    MULTI_PAGE_DETECTION_PROMPT,
    RECEIPT_TOTAL_REVIEW_PROMPT,
    STATEMENT_TOTAL_REVIEW_PROMPT,
    build_arbitration_prompt,
)


class ClaudeClient(AIClient):
    def __init__(self) -> None:
        api_key = get_effective_api_key("anthropic")
        if not api_key:
            raise ValueError("Anthropic API key is not set. Please configure it in the API Keys panel.")
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = settings.claude_model

    def _image_block(self, img_bytes: bytes) -> dict:
        b64 = base64.b64encode(img_bytes).decode()
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        }

    async def extract_invoice(
        self,
        images: list[bytes],
        file_name: str,
        prompt: str | None = None,
    ) -> InvoiceFields:
        content = []
        for img_bytes in images:
            content.append(self._image_block(img_bytes))
        content.append({"type": "text", "text": prompt or EXTRACTION_PROMPT})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            temperature=0.1,
            messages=[{"role": "user", "content": content}],
        )

        text = response.content[0].text
        data = self.parse_json_response(text)
        return self.dict_to_invoice(data, file_name, f"claude:{self.model}")

    async def detect_multi_invoice(self, images: list[bytes]) -> dict:
        content = []
        for i, img_bytes in enumerate(images):
            content.append({"type": "text", "text": f"Page {i + 1}:"})
            content.append(self._image_block(img_bytes))
        content.append({"type": "text", "text": MULTI_PAGE_DETECTION_PROMPT})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            temperature=0.1,
            messages=[{"role": "user", "content": content}],
        )

        text = response.content[0].text
        return self.parse_json_response(text)

    async def arbitrate(
        self,
        images: list[bytes],
        gemini_result: dict,
        openai_result: dict,
        diff_fields: list[str],
        file_name: str,
        *,
        receipt_like: bool = False,
    ) -> InvoiceFields:
        """Use Claude as tiebreaker when Gemini and OpenAI disagree."""
        prompt = build_arbitration_prompt(
            gemini_result,
            openai_result,
            diff_fields,
            receipt_like=receipt_like,
        )

        content = []
        for img_bytes in images:
            content.append(self._image_block(img_bytes))
        content.append({"type": "text", "text": prompt})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            temperature=0.1,
            messages=[{"role": "user", "content": content}],
        )

        text = response.content[0].text
        data = self.parse_json_response(text)
        return self.dict_to_invoice(data, file_name, f"claude-arbitrated:{self.model}")

    async def review_receipt_total(self, images: list[bytes]) -> dict:
        """Ask Claude to determine only the final receipt total amount."""
        content = []
        for img_bytes in images:
            content.append(self._image_block(img_bytes))
        content.append({"type": "text", "text": RECEIPT_TOTAL_REVIEW_PROMPT})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            temperature=0.1,
            messages=[{"role": "user", "content": content}],
        )

        text = response.content[0].text
        return self.parse_json_response(text)

    async def review_statement_total(self, images: list[bytes]) -> dict:
        """Ask Claude to determine only the final total on utility-like statements."""
        content = []
        for img_bytes in images:
            content.append(self._image_block(img_bytes))
        content.append({"type": "text", "text": STATEMENT_TOTAL_REVIEW_PROMPT})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            temperature=0.1,
            messages=[{"role": "user", "content": content}],
        )

        text = response.content[0].text
        return self.parse_json_response(text)
