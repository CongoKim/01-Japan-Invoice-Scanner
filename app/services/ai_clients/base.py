from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from app.models.invoice import InvoiceFields


class AIClient(ABC):
    """Abstract base class for AI invoice extraction clients."""

    @abstractmethod
    async def extract_invoice(
        self,
        images: list[bytes],
        file_name: str,
        prompt: str | None = None,
    ) -> InvoiceFields:
        """Extract invoice fields from one or more page images."""
        ...

    @abstractmethod
    async def detect_multi_invoice(self, images: list[bytes]) -> dict:
        """Detect how many invoices are in a multi-page PDF."""
        ...

    @staticmethod
    def parse_json_response(text: str) -> dict:
        """Extract JSON from AI response text, handling markdown code blocks."""
        # Try to find JSON in code block
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            text = match.group(1)
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            preview = re.sub(r"\s+", " ", text)[:240]
            raise ValueError(
                f"Model response was not valid JSON: {exc.msg}. "
                f"Response preview: {preview}"
            ) from exc

    @staticmethod
    def dict_to_invoice(data: dict, file_name: str, model_name: str) -> InvoiceFields:
        return InvoiceFields(
            file_name=file_name,
            issuer=data.get("issuer"),
            registration_number=data.get("registration_number"),
            issue_date=data.get("issue_date"),
            business_content=data.get("business_content"),
            currency=data.get("currency"),
            remuneration=data.get("remuneration"),
            consumption_tax=data.get("consumption_tax"),
            total_amount=data.get("total_amount"),
            invoice_number=data.get("invoice_number"),
            tax_verification=data.get("tax_verification"),
            withholding_tax=data.get("withholding_tax"),
            source_model=model_name,
        )
