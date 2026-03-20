from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, field_serializer, field_validator

AMOUNT_FIELDS = (
    "remuneration",
    "consumption_tax",
    "total_amount",
    "withholding_tax",
)
TAX_RATES = ("0", "8", "10")


def _coerce_amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if not isinstance(value, str):
        raise ValueError(f"Unsupported amount type: {type(value).__name__}")

    text = unicodedata.normalize("NFKC", value).strip()
    if not text or text.lower() == "null":
        return None

    negative = False
    if text.startswith(("△", "▲")):
        negative = True
        text = text[1:]
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    text = re.sub(r"[\s,，、¥￥円]", "", text)
    text = re.sub(r"^(?:JPY|YEN|\\|Y)+", "", text, flags=re.IGNORECASE)
    if text.startswith("-"):
        negative = True
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]

    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        raise ValueError(f"Invalid amount value: {value!r}")

    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount value: {value!r}") from exc
    return -amount if negative else amount


def _serialize_amount(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _normalize_tax_verification(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)

    text = unicodedata.normalize("NFKC", value).strip()
    if not text or text.lower() == "null":
        return None

    matches = re.findall(r"(0|8|10)\s*%?\s*[:：]\s*([+-]?\d[\d,，、]*)\s*円?", text)
    if not matches:
        return text

    values = {rate: "0" for rate in TAX_RATES}
    for rate, amount in matches:
        normalized_amount = re.sub(r"[^\d+-]", "", amount)
        if normalized_amount in {"", "+", "-"}:
            continue
        if normalized_amount.startswith("+"):
            normalized_amount = normalized_amount[1:]
        values[rate] = normalized_amount

    return "; ".join(f"{rate}%: {values[rate]}円" for rate in TAX_RATES)


class InvoiceFields(BaseModel):
    file_name: str = ""
    issuer: str | None = None                 # 相手先（开票方）
    registration_number: str | None = None    # 登録番号 (T+13位)
    issue_date: str | None = None             # 発行日 (YYYY-MM-DD)
    business_content: str | None = None       # 業務内容
    currency: str | None = None               # 币种
    remuneration: Decimal | None = None       # 報酬額（税前）
    consumption_tax: Decimal | None = None    # 消費税額
    total_amount: Decimal | None = None       # 総額
    invoice_number: str | None = None         # 發票號
    tax_verification: str | None = None       # 消费税核定
    withholding_tax: Decimal | None = None    # 源泉税金額
    source_model: str | None = None           # 最终采用的模型
    error: str | None = None                  # 处理错误信息
    gemini_error: str | None = None           # Gemini 模型错误
    openai_error: str | None = None           # OpenAI 模型错误

    @field_validator(*AMOUNT_FIELDS, mode="before")
    @classmethod
    def validate_amounts(cls, value: Any) -> Decimal | None:
        return _coerce_amount(value)

    @field_serializer(*AMOUNT_FIELDS, when_used="json")
    def serialize_amounts(self, value: Decimal | None) -> int | float | None:
        return _serialize_amount(value)

    @field_validator("tax_verification", mode="before")
    @classmethod
    def validate_tax_verification(cls, value: Any) -> str | None:
        return _normalize_tax_verification(value)
