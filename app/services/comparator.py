"""Compare results from Gemini and OpenAI, invoke Claude arbitration if needed."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from app.models.invoice import InvoiceFields
from app.services.ai_clients.claude import ClaudeClient

# Fields to compare (business_content excluded per requirements)
COMPARE_FIELDS = [
    "issuer", "registration_number", "issue_date", "currency",
    "remuneration", "consumption_tax", "total_amount",
    "invoice_number", "tax_verification", "withholding_tax",
]

DATE_PATTERNS = (
    re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$"),
)


def normalize_date(value: str) -> str:
    for pattern in DATE_PATTERNS:
        match = pattern.fullmatch(value)
        if match:
            year, month, day = (int(part) for part in match.groups())
            return f"{year:04d}-{month:02d}-{day:02d}"
    return value


def _normalize_number(value: Decimal | int | float) -> str:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(decimal_value.normalize(), "f")


def normalize(value: str | int | float | Decimal | None) -> str | None:
    """Normalize a value for comparison: full-width → half-width, strip spaces/commas."""
    if value is None:
        return None
    if isinstance(value, (Decimal, int, float)):
        return _normalize_number(value)
    # Full-width to half-width
    value = unicodedata.normalize("NFKC", value)
    # Remove spaces, commas, yen signs
    value = re.sub(r"[\s,，、¥￥円]", "", value)
    # Normalize dashes
    value = re.sub(r"[ー－—–]", "-", value)
    value = normalize_date(value)
    return value.strip()


def find_diff_fields(a: InvoiceFields, b: InvoiceFields) -> list[str]:
    """Return list of field names where a and b disagree."""
    diffs = []
    for field in COMPARE_FIELDS:
        val_a = normalize(getattr(a, field))
        val_b = normalize(getattr(b, field))
        # Both None → agree
        if val_a is None and val_b is None:
            continue
        # One None, one not → disagree
        if val_a is None or val_b is None:
            diffs.append(field)
            continue
        if val_a != val_b:
            diffs.append(field)
    return diffs


async def compare_and_arbitrate(
    gemini_result: InvoiceFields,
    openai_result: InvoiceFields,
    images: list[bytes],
    file_name: str,
    claude_client: ClaudeClient,
    *,
    receipt_like: bool = False,
) -> InvoiceFields:
    """Compare Gemini and OpenAI results.

    If all fields match → return Gemini result.
    If fields differ → invoke Claude arbitration.
    """
    diff_fields = find_diff_fields(gemini_result, openai_result)

    if not diff_fields:
        # Results agree — use Gemini result, mark source
        gemini_result.source_model = f"agreed({gemini_result.source_model})"
        return gemini_result

    # Disagreement — Claude arbitrates
    gemini_dict = gemini_result.model_dump(
        mode="json",
        exclude={"file_name", "source_model", "error", "gemini_error", "openai_error"}
    )
    openai_dict = openai_result.model_dump(
        mode="json",
        exclude={"file_name", "source_model", "error", "gemini_error", "openai_error"}
    )

    result = await claude_client.arbitrate(
        images=images,
        gemini_result=gemini_dict,
        openai_result=openai_dict,
        diff_fields=diff_fields,
        file_name=file_name,
        receipt_like=receipt_like,
    )
    return result
