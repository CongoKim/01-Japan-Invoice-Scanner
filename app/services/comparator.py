"""Compare results from Gemini and OpenAI, invoke Claude arbitration if needed."""

from __future__ import annotations

import logging
import re
import unicodedata
from decimal import Decimal

from app.models.invoice import InvoiceFields
from app.services.ai_clients.claude import ClaudeClient

logger = logging.getLogger(__name__)

# Fields to compare (business_content excluded per requirements)
COMPARE_FIELDS = [
    "issuer", "registration_number", "issue_date", "currency",
    "remuneration", "consumption_tax", "total_amount",
    "invoice_number", "tax_verification", "withholding_tax",
]

AMOUNT_DIFF_FIELDS = {"remuneration", "consumption_tax", "total_amount", "withholding_tax"}

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


def is_amount_consistent(result: InvoiceFields) -> bool:
    """Check if remuneration + consumption_tax == total_amount."""
    r = result.remuneration
    t = result.consumption_tax
    total = result.total_amount
    if r is None or t is None or total is None:
        return False
    return total == r + t


def check_amount_consistency(result: InvoiceFields) -> list[str]:
    """Validate mathematical relationships between amount fields.

    総額 is defined as the pre-withholding tax-inclusive total
    (報酬額 + 消費税), NOT the post-withholding 差引支払額.
    If 総額 equals 報酬 + 消費税 - 源泉税, it likely means the model
    extracted 差引支払額 as 総額, which should be flagged.

    Returns a list of warning strings (empty if consistent).
    """
    warnings: list[str] = []
    r = result.remuneration
    t = result.consumption_tax
    total = result.total_amount
    w = result.withholding_tax

    if r is not None and t is not None and total is not None:
        expected = r + t
        if total != expected:
            # Detect the specific case where 総額 looks like 差引支払額
            if w and total == expected - abs(w):
                warnings.append(
                    f"総額が差引支払額の可能性: 報酬{r} + 税{t} = {expected}"
                    f" だが総額は{total}（= {expected} - 源泉{abs(w)}）"
                )
            else:
                warnings.append(
                    f"金額不整合: 報酬{r} + 税{t} = {expected} ≠ 総額{total}"
                )

    if r is not None and t is not None and r > 0 and t > 0:
        tax_rate = t / r
        if not (Decimal("0.079") <= tax_rate <= Decimal("0.101")):
            warnings.append(
                f"消費税率が異常: {t}/{r} = {float(tax_rate):.4f}"
            )

    return warnings


# Tax rate factors for deriving consumption_tax from tax_verification
_TAX_RATE_FACTORS = {"0": Decimal("0"), "8": Decimal("0.08"), "10": Decimal("0.10")}
_TAX_VERIFICATION_PATTERN = re.compile(
    r"(0|8|10)\s*%?\s*[:：]\s*([+-]?\d[\d,，、]*)\s*円?"
)


def maybe_fill_consumption_tax(result: InvoiceFields) -> bool:
    """Try to derive consumption_tax from tax_verification when it is missing.

    Uses the tax_verification breakdown to compute tax via SUMPRODUCT
    (tax-base mode) or direct sum (receipt tax-amount mode), and fills
    consumption_tax if the derivation is unambiguous.

    Returns True if consumption_tax was filled.
    """
    if result.consumption_tax is not None:
        return False
    if not result.tax_verification:
        return False

    matches = _TAX_VERIFICATION_PATTERN.findall(result.tax_verification)
    if not matches:
        return False

    breakdown: dict[str, Decimal] = {rate: Decimal("0") for rate in _TAX_RATE_FACTORS}
    for rate, amount_str in matches:
        normalized = re.sub(r"[^\d+-]", "", amount_str)
        if normalized in {"", "+", "-"}:
            continue
        breakdown[rate] = Decimal(normalized)

    # SUMPRODUCT: treat values as tax bases → multiply by rate to get tax
    sumproduct_total = sum(
        amount * _TAX_RATE_FACTORS[rate] for rate, amount in breakdown.items()
    )
    # Direct sum: treat values as tax amounts themselves
    direct_sum_total = sum(breakdown.values(), Decimal("0"))

    # If total_amount and remuneration are available, pick the interpretation
    # that makes the amounts consistent (total = remuneration + tax)
    r = result.remuneration
    total = result.total_amount
    if r is not None and total is not None:
        sp_consistent = (total == r + sumproduct_total)
        ds_consistent = (total == r + direct_sum_total)
        if sp_consistent and not ds_consistent:
            result.consumption_tax = sumproduct_total
        elif ds_consistent and not sp_consistent:
            result.consumption_tax = direct_sum_total
        else:
            # Both or neither consistent — use sumproduct as default
            # (invoice-style tax base is more common)
            result.consumption_tax = sumproduct_total
    else:
        # No other amounts to cross-check; use sumproduct as default
        result.consumption_tax = sumproduct_total

    logger.info(
        "Filled consumption_tax=%s from tax_verification for %s",
        result.consumption_tax,
        result.file_name,
    )
    return True


def _only_amount_diffs(diff_fields: list[str]) -> bool:
    """Return True if all diff fields are amount fields."""
    return bool(diff_fields) and all(f in AMOUNT_DIFF_FIELDS for f in diff_fields)


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
    If only amount fields differ and one side is mathematically consistent →
    prefer that side without Claude arbitration.
    Otherwise → invoke Claude arbitration.
    """
    diff_fields = find_diff_fields(gemini_result, openai_result)

    if not diff_fields:
        # Results agree — use Gemini result, mark source
        gemini_result.source_model = f"agreed({gemini_result.source_model})"
        return gemini_result

    # When only amount fields differ, prefer the mathematically consistent side
    if _only_amount_diffs(diff_fields):
        g_consistent = is_amount_consistent(gemini_result)
        o_consistent = is_amount_consistent(openai_result)
        if g_consistent and not o_consistent:
            logger.info(
                "%s: amount diffs %s resolved by consistency → gemini",
                file_name, diff_fields,
            )
            gemini_result.source_model = (
                f"consistent-gemini({gemini_result.source_model})"
            )
            return gemini_result
        if o_consistent and not g_consistent:
            logger.info(
                "%s: amount diffs %s resolved by consistency → openai",
                file_name, diff_fields,
            )
            openai_result.source_model = (
                f"consistent-openai({openai_result.source_model})"
            )
            return openai_result

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
