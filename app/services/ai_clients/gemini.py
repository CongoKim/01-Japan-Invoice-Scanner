from __future__ import annotations

from google import genai
from google.genai import types

from app.config import get_effective_api_key, settings
from app.models.invoice import InvoiceFields
from app.services.ai_clients.base import AIClient
from app.services.prompt import EXTRACTION_PROMPT, MULTI_PAGE_DETECTION_PROMPT


class GeminiClient(AIClient):
    def __init__(self) -> None:
        api_key = get_effective_api_key("gemini")
        if not api_key:
            raise ValueError("Gemini API key is not set. Please configure it in the API Keys panel.")
        self.client = genai.Client(api_key=api_key)
        self.model = settings.gemini_model

    async def extract_invoice(
        self,
        images: list[bytes],
        file_name: str,
        prompt: str | None = None,
    ) -> InvoiceFields:
        parts = []
        for img_bytes in images:
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))
        parts.append(types.Part.from_text(text=prompt or EXTRACTION_PROMPT))

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=4096,
            ),
        )

        data = self.parse_json_response(response.text)
        return self.dict_to_invoice(data, file_name, f"gemini:{self.model}")

    async def detect_multi_invoice(self, images: list[bytes]) -> dict:
        parts = []
        for i, img_bytes in enumerate(images):
            parts.append(types.Part.from_text(text=f"Page {i + 1}:"))
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))
        parts.append(types.Part.from_text(text=MULTI_PAGE_DETECTION_PROMPT))

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )

        return self.parse_json_response(response.text)
