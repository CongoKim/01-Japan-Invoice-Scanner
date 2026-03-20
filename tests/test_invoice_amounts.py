from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from app.models.invoice import InvoiceFields
from app.services.ai_clients.base import AIClient
from app.services.comparator import normalize
from app.services.excel_writer import write_excel


def test_invoice_fields_coerce_amounts_to_decimal():
    invoice = InvoiceFields(
        remuneration="¥1,234",
        consumption_tax="Y80",
        total_amount="\\3,860.50",
        withholding_tax="(JPY2960)",
    )

    assert invoice.remuneration == Decimal("1234")
    assert invoice.consumption_tax == Decimal("80")
    assert invoice.total_amount == Decimal("3860.50")
    assert invoice.withholding_tax == Decimal("-2960")


def test_dict_to_invoice_accepts_numeric_json_amounts():
    invoice = AIClient.dict_to_invoice(
        {
            "currency": "JPY",
            "remuneration": 1000,
            "consumption_tax": 100,
            "total_amount": 1100,
            "withholding_tax": 0,
        },
        "sample.jpg",
        "gemini:test",
    )

    assert invoice.remuneration == Decimal("1000")
    assert invoice.consumption_tax == Decimal("100")
    assert invoice.total_amount == Decimal("1100")
    assert invoice.withholding_tax == Decimal("0")


def test_excel_writer_outputs_numeric_cells(tmp_path: Path):
    output_path = tmp_path / "invoice.xlsx"
    write_excel(
        [
            InvoiceFields(
                file_name="sample.jpg",
                remuneration="1000",
                consumption_tax="0",
                total_amount="1100.5",
                withholding_tax="-2960",
            )
        ],
        output_path,
    )

    wb = load_workbook(output_path)
    ws = wb.active
    assert ws["G2"].value == 1000
    assert ws["H2"].value == 0
    assert ws["I2"].value == 1100.5
    assert ws["L2"].value == -2960
    assert ws["G2"].number_format == "#,##0.########"


def test_excel_writer_tax_verification_uses_sumproduct_for_tax_base_detail(tmp_path: Path):
    output_path = tmp_path / "invoice.xlsx"
    write_excel(
        [
            InvoiceFields(
                file_name="sample.pdf",
                consumption_tax="9630",
                tax_verification="0%: 1710円; 10%: 96300円",
            )
        ],
        output_path,
    )

    wb = load_workbook(output_path)
    ws = wb.active
    assert ws["K2"].value == 9630
    assert ws["K2"].number_format == "#,##0.########"
    assert ws["K2"].comment is not None
    assert "SUMPRODUCT" in ws["K2"].comment.text


def test_excel_writer_tax_verification_stays_numeric_for_receipt_tax_amount_detail(tmp_path: Path):
    output_path = tmp_path / "invoice.xlsx"
    write_excel(
        [
            InvoiceFields(
                file_name="receipt.jpg",
                consumption_tax="92",
                tax_verification="8%: 92円",
            )
        ],
        output_path,
    )

    wb = load_workbook(output_path)
    ws = wb.active
    assert ws["K2"].value == 92
    assert ws["K2"].comment is not None
    assert "票面税額" in ws["K2"].comment.text


def test_normalize_supports_numeric_values():
    assert normalize(Decimal("3860")) == "3860"
    assert normalize(3860) == "3860"
    assert normalize("3,860円") == "3860"


def test_tax_verification_is_normalized_to_all_rates():
    invoice = InvoiceFields(tax_verification="10%: 96300円; 0%: 1710円")

    assert invoice.tax_verification == "0%: 1710円; 8%: 0円; 10%: 96300円"
