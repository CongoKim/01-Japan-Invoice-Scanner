import io
from pathlib import Path

from PIL import Image

from app.models.invoice import InvoiceFields
from app.services.orchestrator import (
    _prepare_images,
    _should_review_receipt_total,
    _should_review_statement_total,
)
from app.services.prompt import (
    EXTRACTION_PROMPT,
    RECEIPT_TOTAL_REVIEW_PROMPT,
    STATEMENT_TOTAL_REVIEW_PROMPT,
    build_extraction_prompt,
    build_arbitration_prompt,
)


def _write_jpeg(path: Path, size: tuple[int, int]) -> None:
    Image.new("RGB", size, color="white").save(path, format="JPEG")


def test_prepare_images_adds_receipt_views_for_receipt_like_image(tmp_path: Path):
    image_path = tmp_path / "receipt.jpg"
    _write_jpeg(image_path, (800, 320))

    images = _prepare_images(image_path)

    assert len(images) == 5
    decoded_sizes = [Image.open(io.BytesIO(data)).size for data in images]
    assert decoded_sizes[0] == (800, 320)
    assert decoded_sizes[1][0] > decoded_sizes[0][0]
    assert decoded_sizes[2][0] < decoded_sizes[1][0]
    assert decoded_sizes[2][1] < decoded_sizes[1][1]
    assert decoded_sizes[3][1] < decoded_sizes[1][1]
    assert decoded_sizes[4][0] < decoded_sizes[1][0]
    assert decoded_sizes[4][1] < decoded_sizes[1][1]


def test_prepare_images_keeps_single_view_for_regular_image(tmp_path: Path):
    image_path = tmp_path / "invoice.jpg"
    _write_jpeg(image_path, (1200, 1200))

    images = _prepare_images(image_path)

    assert len(images) == 1


def test_amount_prompt_mentions_total_labels_and_exclusions():
    assert "優先ラベル：合計" in EXTRACTION_PROMPT
    assert "電話番号" in EXTRACTION_PROMPT
    assert "手書きの Y" in EXTRACTION_PROMPT
    assert "税額ではありません" in EXTRACTION_PROMPT
    assert "発票全体の金額構造を理解" in EXTRACTION_PROMPT
    invoice_prompt = build_extraction_prompt(False)
    receipt_prompt = build_extraction_prompt(True)
    assert "金額基数" in invoice_prompt
    assert "税率別税額" in receipt_prompt
    assert "安易に推定しない" in receipt_prompt
    prompt = build_arbitration_prompt({}, {}, ["total_amount"], receipt_like=True)
    assert "領収金額" in prompt
    assert "注文番号" in prompt
    assert "税率別税額" in prompt
    assert "公共料金や請求明細書" in RECEIPT_TOTAL_REVIEW_PROMPT
    assert "右下の金額列" in RECEIPT_TOTAL_REVIEW_PROMPT
    assert "先頭の数字として数えない" in RECEIPT_TOTAL_REVIEW_PROMPT
    assert "ご請求予定額" in STATEMENT_TOTAL_REVIEW_PROMPT
    assert "ガス料金" in STATEMENT_TOTAL_REVIEW_PROMPT


def test_sparse_receipt_result_triggers_total_review():
    invoice = InvoiceFields(
        issuer="株式会社大天元",
        registration_number="T3040001101237",
        issue_date="2025-01-11",
        currency="JPY",
        total_amount=73860,
    )

    assert _should_review_receipt_total(True, invoice) is True
    assert _should_review_receipt_total(False, invoice) is False


def test_suspicious_receipt_result_triggers_total_review_even_when_many_fields_present():
    invoice = InvoiceFields(
        issuer="JINJIN-JINJIN株式会社",
        registration_number="T1234567890123",
        issue_date="2020-07-25",
        business_content="コンサルティング業務",
        currency="JPY",
        total_amount=198000,
        invoice_number="2255",
    )

    assert _should_review_receipt_total(True, invoice) is True


def test_utility_bill_result_triggers_statement_total_review():
    invoice = InvoiceFields(
        issuer="東京ガス株式会社",
        business_content="ガス料金 暖トクセット; 電気料金 基本A",
        total_amount=39262,
    )

    assert _should_review_statement_total(invoice) is True
