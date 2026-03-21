"""Core pipeline orchestrator: ZIP -> extract -> AI OCR -> compare -> Excel."""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from pathlib import Path

from app.config import has_effective_api_key, settings
from app.models.invoice import InvoiceFields
from app.models.task import task_store
from app.services.ai_clients.claude import ClaudeClient
from app.services.ai_clients.gemini import GeminiClient
from app.services.ai_clients.openai_client import OpenAIClient
from app.services.comparator import (
    check_amount_consistency,
    compare_and_arbitrate,
    maybe_fill_consumption_tax,
)
from app.services.excel_writer import write_excel
from app.services.extractor import extract_zip, is_pdf
from app.services.pdf_processor import render_pdf_pages
from app.services.prompt import build_extraction_prompt
from app.services.task_runtime import get_task_output_path

logger = logging.getLogger(__name__)

UTILITY_TOTAL_KEYWORDS = (
    "東京ガス",
    "東京都水道局",
    "水道料金",
    "下水道料金",
    "ガス料金",
    "電気料金",
    "公共料金",
)
PLACEHOLDER_REGISTRATION_NUMBERS = {"T1234567890123"}


def _track_call(task_id: str, model: str, call_type: str) -> None:
    """Increment model call counter for the given task."""
    task = task_store.get(task_id)
    if task and model in task.model_calls:
        task.model_calls[model][call_type] = (
            task.model_calls[model].get(call_type, 0) + 1
        )


ANALYSIS_CONCURRENCY_LIMIT = 4
WORK_QUEUE_MULTIPLIER = 2
MAX_RETRIES = 4
SINGLE_MODEL_REVIEW_FIELDS = (
    "issuer",
    "registration_number",
    "issue_date",
    "business_content",
    "remuneration",
    "consumption_tax",
    "total_amount",
    "invoice_number",
    "withholding_tax",
)
SINGLE_MODEL_CONTEXT_FIELDS = (
    "business_content",
    "remuneration",
    "consumption_tax",
    "invoice_number",
    "withholding_tax",
)
MIN_SINGLE_MODEL_CONFIDENCE_FIELDS = 5
MAX_RECEIPT_TOTAL_REVIEW_FIELDS = 5

WorkItem = tuple[int, int, str, list[bytes], bool]
AnalysisOutcome = tuple[int, str, list[WorkItem], str | None]

# Lazy-init clients (created on first use to avoid import-time API calls)
_gemini: GeminiClient | None = None
_openai: OpenAIClient | None = None
_claude: ClaudeClient | None = None


def _get_clients() -> tuple[GeminiClient, OpenAIClient, ClaudeClient]:
    global _gemini, _openai, _claude
    missing = []
    if not has_effective_api_key("gemini"):
        missing.append("Gemini")
    if not has_effective_api_key("openai"):
        missing.append("OpenAI")
    if not has_effective_api_key("anthropic"):
        missing.append("Anthropic")
    if missing:
        raise ValueError(
            f"缺少 API 密钥：{', '.join(missing)}。"
            "请先在 API 密钥面板中完成配置。"
        )

    if _gemini is None:
        _gemini = GeminiClient()
    if _openai is None:
        _openai = OpenAIClient()
    if _claude is None:
        _claude = ClaudeClient()
    return _gemini, _openai, _claude


def reset_clients() -> None:
    """Force re-initialization of all AI clients (call after API key update)."""
    global _gemini, _openai, _claude
    _gemini = None
    _openai = None
    _claude = None


async def _call_with_retry(coro_func, max_retries: int = MAX_RETRIES, backoff: float = 2.0):
    """Call an async function with exponential backoff retry."""
    for attempt in range(max_retries):
        try:
            return await coro_func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = backoff ** attempt
            retry_after = _extract_retry_delay_seconds(e)
            if retry_after is not None:
                wait = max(wait, retry_after + 0.5)
            logger.warning(f"Retry {attempt + 1}/{max_retries} after {wait}s: {e}")
            await asyncio.sleep(wait)


def _compact_exception(exc: Exception) -> str:
    message = re.sub(r"\s+", " ", str(exc)).strip()
    return message[:240]


def _extract_retry_delay_seconds(exc: Exception) -> float | None:
    message = str(exc)
    match = re.search(r"try again in\s+([\d.]+)\s*(ms|s)\b", message, re.IGNORECASE)
    if not match:
        return None

    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "ms":
        return value / 1000
    return value


def _attach_model_errors(
    result: InvoiceFields,
    *,
    gemini_error: str | None = None,
    openai_error: str | None = None,
) -> InvoiceFields:
    result.gemini_error = gemini_error
    result.openai_error = openai_error
    return result


def _count_present_fields(result: InvoiceFields, fields: tuple[str, ...]) -> int:
    return sum(
        1
        for field in fields
        if (value := getattr(result, field)) is not None and value != ""
    )


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "")


def _has_suspicious_total_context(result: InvoiceFields) -> bool:
    if result.total_amount is None:
        return False

    if result.registration_number in PLACEHOLDER_REGISTRATION_NUMBERS:
        return True

    invoice_number = _normalize_text(result.invoice_number)
    if invoice_number:
        digits_only = re.sub(r"\D", "", invoice_number)
        if len(digits_only) <= 4 and result.total_amount >= 10000:
            return True

    lacks_amount_context = not any(
        (value := getattr(result, field)) is not None and value != ""
        for field in SINGLE_MODEL_CONTEXT_FIELDS
    )
    if lacks_amount_context and result.total_amount >= 10000:
        return True

    return False


def _should_review_single_model(result: InvoiceFields) -> bool:
    present_fields = _count_present_fields(result, SINGLE_MODEL_REVIEW_FIELDS)
    lacks_amount_context = result.total_amount is not None and not any(
        (value := getattr(result, field)) is not None and value != ""
        for field in SINGLE_MODEL_CONTEXT_FIELDS
    )
    return (
        present_fields < MIN_SINGLE_MODEL_CONFIDENCE_FIELDS
        or lacks_amount_context
    )


async def _review_single_model_result(
    *,
    task_id: str,
    file_label: str,
    images: list[bytes],
    receipt_like: bool,
    claude: ClaudeClient,
    fallback_result: InvoiceFields,
    fallback_source_prefix: str,
    gemini_error: str | None = None,
    openai_error: str | None = None,
) -> InvoiceFields:
    if not _should_review_single_model(fallback_result):
        fallback_result.source_model = f"{fallback_source_prefix}:{fallback_result.source_model}"
        return _attach_model_errors(
            fallback_result,
            gemini_error=gemini_error,
            openai_error=openai_error,
        )

    try:
        _track_call(task_id, "claude", "extract")
        reviewed_result = await _call_with_retry(
            lambda: claude.extract_invoice(
                images,
                file_label,
                prompt=build_extraction_prompt(receipt_like),
            )
        )
    except Exception as review_error:
        logger.warning(
            f"Claude review failed for sparse single-model result {file_label}: "
            f"{review_error}"
        )
        fallback_result.source_model = (
            f"{fallback_source_prefix}:{fallback_result.source_model}"
        )
        return _attach_model_errors(
            fallback_result,
            gemini_error=gemini_error,
            openai_error=openai_error,
        )

    reviewed_result.source_model = f"claude-review:{reviewed_result.source_model}"
    return _attach_model_errors(
        reviewed_result,
        gemini_error=gemini_error,
        openai_error=openai_error,
    )


def _should_review_receipt_total(receipt_like: bool, result: InvoiceFields) -> bool:
    if not receipt_like or result.total_amount is None:
        return False
    present_fields = _count_present_fields(result, SINGLE_MODEL_REVIEW_FIELDS)
    return (
        present_fields <= MAX_RECEIPT_TOTAL_REVIEW_FIELDS
        or _has_suspicious_total_context(result)
    )


def _should_review_statement_total(result: InvoiceFields) -> bool:
    if result.total_amount is None:
        return False

    combined = _normalize_text(result.issuer) + _normalize_text(result.business_content)
    return any(keyword in combined for keyword in UTILITY_TOTAL_KEYWORDS)


async def _maybe_review_receipt_total(
    *,
    task_id: str,
    file_label: str,
    images: list[bytes],
    receipt_like: bool,
    result: InvoiceFields,
    claude: ClaudeClient,
) -> InvoiceFields:
    if not _should_review_receipt_total(receipt_like, result):
        return result

    try:
        _track_call(task_id, "claude", "review_receipt")
        review_data = await _call_with_retry(
            lambda: claude.review_receipt_total(images)
        )
    except Exception as review_error:
        logger.warning(
            f"Receipt total review failed for {file_label}: {review_error}"
        )
        return result

    reviewed_raw_amount = review_data.get("total_amount")
    if reviewed_raw_amount is None or reviewed_raw_amount == "":
        return result

    try:
        reviewed_amount = InvoiceFields(total_amount=reviewed_raw_amount).total_amount
    except Exception as review_error:
        logger.warning(
            f"Receipt total review returned invalid amount for {file_label}: "
            f"{review_error}"
        )
        return result

    if reviewed_amount is None or reviewed_amount == result.total_amount:
        return result

    logger.info(
        "Receipt total review adjusted %s from %s to %s",
        file_label,
        result.total_amount,
        reviewed_amount,
    )
    result.total_amount = reviewed_amount
    if result.source_model:
        result.source_model = f"{result.source_model}+receipt-total-review"
    else:
        result.source_model = "receipt-total-review"
    return result


async def _maybe_review_statement_total(
    *,
    task_id: str,
    file_label: str,
    images: list[bytes],
    result: InvoiceFields,
    claude: ClaudeClient,
) -> InvoiceFields:
    if not _should_review_statement_total(result):
        return result

    try:
        _track_call(task_id, "claude", "review_receipt")
        review_data = await _call_with_retry(
            lambda: claude.review_statement_total(images)
        )
    except Exception as review_error:
        logger.warning(
            f"Statement total review failed for {file_label}: {review_error}"
        )
        return result

    reviewed_raw_amount = review_data.get("total_amount")
    if reviewed_raw_amount is None or reviewed_raw_amount == "":
        return result

    try:
        reviewed_amount = InvoiceFields(total_amount=reviewed_raw_amount).total_amount
    except Exception as review_error:
        logger.warning(
            f"Statement total review returned invalid amount for {file_label}: "
            f"{review_error}"
        )
        return result

    if reviewed_amount is None or reviewed_amount == result.total_amount:
        return result

    logger.info(
        "Statement total review adjusted %s from %s to %s",
        file_label,
        result.total_amount,
        reviewed_amount,
    )
    result.total_amount = reviewed_amount
    if result.source_model:
        result.source_model = f"{result.source_model}+statement-total-review"
    else:
        result.source_model = "statement-total-review"
    return result


async def _apply_total_reviews(
    *,
    task_id: str,
    file_label: str,
    images: list[bytes],
    receipt_like: bool,
    result: InvoiceFields,
    claude: ClaudeClient,
) -> InvoiceFields:
    result = await _maybe_review_receipt_total(
        task_id=task_id,
        file_label=file_label,
        images=images,
        receipt_like=receipt_like,
        result=result,
        claude=claude,
    )
    return await _maybe_review_statement_total(
        task_id=task_id,
        file_label=file_label,
        images=images,
        result=result,
        claude=claude,
    )


async def _wait_until_active(task_id: str):
    """Wait until task is active again, or return None if it was cancelled."""
    while True:
        task = task_store.get(task_id)
        if not task or task.status == "cancelled":
            return None
        if task.status != "paused":
            return task
        await asyncio.sleep(0.2)


def _prepare_images(file_path: Path) -> list[bytes]:
    """Convert a file (image or PDF) into a list of PNG-encoded page images.

    All outputs are PNG so every AI client receives a consistent MIME type.
    """
    if is_pdf(file_path):
        return render_pdf_pages(file_path)

    # Normalize image to PNG via Pillow (handles JPEG, TIFF, BMP, WebP, HEIC, etc.)
    import io
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    # Register HEIC/HEIF support if available
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass

    def encode_png(image: Image.Image) -> bytes:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    def is_receipt_like(image: Image.Image) -> bool:
        width, height = image.size
        long_side = max(width, height)
        short_side = min(width, height)
        aspect_ratio = long_side / max(short_side, 1)
        return aspect_ratio >= 1.35 and short_side <= 900

    def build_enhanced_view(image: Image.Image) -> Image.Image:
        grayscale = ImageOps.grayscale(image)
        autocontrast = ImageOps.autocontrast(grayscale, cutoff=1)
        sharpened = autocontrast.filter(ImageFilter.SHARPEN)
        boosted = ImageEnhance.Contrast(sharpened).enhance(1.25)
        if min(boosted.size) < 1200:
            scale = max(2, int(1200 / max(min(boosted.size), 1)))
            boosted = boosted.resize(
                (boosted.width * scale, boosted.height * scale),
                Image.Resampling.LANCZOS,
            )
        return boosted.convert("RGB")

    def build_receipt_focus_crop(image: Image.Image) -> Image.Image | None:
        width, height = image.size
        if width < 200 or height < 200:
            return None
        left = int(width * 0.08)
        right = int(width * 0.92)
        top = int(height * 0.52)
        bottom = int(height * 0.98)
        if right - left < 80 or bottom - top < 80:
            return None
        return image.crop((left, top, right, bottom))

    def build_receipt_amount_column_crop(image: Image.Image) -> Image.Image | None:
        width, height = image.size
        if width < 200 or height < 200:
            return None
        left = int(width * 0.42)
        right = int(width * 0.98)
        top = int(height * 0.48)
        bottom = int(height * 0.98)
        if right - left < 80 or bottom - top < 80:
            return None
        return image.crop((left, top, right, bottom))

    def build_receipt_summary_crop(image: Image.Image) -> Image.Image | None:
        width, height = image.size
        if width < 200 or height < 200:
            return None
        left = int(width * 0.08)
        right = int(width * 0.92)
        top = int(height * 0.12)
        bottom = int(height * 0.68)
        if right - left < 80 or bottom - top < 80:
            return None
        return image.crop((left, top, right, bottom))

    with Image.open(file_path) as img:
        base_image = img.convert("RGB")
        images = [encode_png(base_image)]

        if is_receipt_like(base_image):
            enhanced_image = build_enhanced_view(base_image)
            images.append(encode_png(enhanced_image))
            summary_crop = build_receipt_summary_crop(enhanced_image)
            if summary_crop is not None:
                images.append(encode_png(summary_crop))
            focus_crop = build_receipt_focus_crop(enhanced_image)
            if focus_crop is not None:
                images.append(encode_png(focus_crop))
            amount_column_crop = build_receipt_amount_column_crop(enhanced_image)
            if amount_column_crop is not None:
                images.append(encode_png(amount_column_crop))

        return images


def _pick_detection(
    gemini_det: dict | Exception | None,
    openai_det: dict | Exception | None,
) -> dict | None:
    """Pick the best multi-page detection result from two models.

    Strategy: if both succeed, prefer the one with more invoices (safer to
    over-split than to miss).  If only one succeeds, use that one.
    """
    g_ok = isinstance(gemini_det, dict)
    o_ok = isinstance(openai_det, dict)
    if not g_ok and not o_ok:
        return None
    if g_ok and not o_ok:
        return gemini_det
    if o_ok and not g_ok:
        return openai_det
    # Both succeeded — pick the one that found more invoices
    g_count = gemini_det.get("invoice_count", 1)
    o_count = openai_det.get("invoice_count", 1)
    if o_count > g_count:
        return openai_det
    return gemini_det


async def _handle_multi_page_pdf(
    task_id: str,
    file_path: Path,
    page_images: list[bytes],
    gemini: GeminiClient,
    openai_client: OpenAIClient,
) -> list[tuple[str, list[bytes]]]:
    """For multi-page PDFs, detect if it contains multiple invoices.

    Runs Gemini and OpenAI detection in parallel for cross-validation.
    """
    if len(page_images) == 1:
        return [(file_path.name, page_images)]

    try:
        _track_call(task_id, "gemini", "detect_multi")
        _track_call(task_id, "openai", "detect_multi")
        gemini_det, openai_det = await asyncio.gather(
            _call_with_retry(
                lambda: gemini.detect_multi_invoice(page_images)
            ),
            _call_with_retry(
                lambda: openai_client.detect_multi_invoice(page_images)
            ),
            return_exceptions=True,
        )

        if isinstance(gemini_det, Exception):
            logger.warning(
                f"Gemini multi-page detection failed for {file_path.name}: {gemini_det}"
            )
        if isinstance(openai_det, Exception):
            logger.warning(
                f"OpenAI multi-page detection failed for {file_path.name}: {openai_det}"
            )

        detection = _pick_detection(gemini_det, openai_det)
        if detection is None:
            logger.warning(
                f"Both models failed multi-page detection for {file_path.name}"
            )
            return [(file_path.name, page_images)]

        invoice_count = detection.get("invoice_count", 1)
        invoices_info = detection.get("invoices", [])

        if invoice_count <= 1 or not invoices_info:
            return [(file_path.name, page_images)]

        result = []
        for inv in invoices_info:
            idx = inv.get("invoice_index", 1)
            pages = inv.get("pages", [])
            imgs = [page_images[p - 1] for p in pages if 1 <= p <= len(page_images)]
            if imgs:
                result.append((f"{file_path.name}_invoice{idx}", imgs))
        return result or [(file_path.name, page_images)]

    except Exception as e:
        logger.warning(f"Multi-page detection failed for {file_path.name}: {e}")
        return [(file_path.name, page_images)]


async def _analyze_file(
    task_id: str,
    file_index: int,
    file_path: Path,
    gemini: GeminiClient,
    openai_client: OpenAIClient,
) -> list[WorkItem]:
    """Analyze a single file and return zero or more work items."""
    task = await _wait_until_active(task_id)
    if not task:
        return []

    task.current_file = f"正在分析：{file_path.name}"
    task_store.notify(task_id)

    page_images = await asyncio.to_thread(_prepare_images, file_path)

    task = await _wait_until_active(task_id)
    if not task:
        return []

    receipt_like = not is_pdf(file_path) and len(page_images) > 1

    if is_pdf(file_path) and len(page_images) > 1:
        analyzed_items = [
            (label, images, False)
            for label, images in await _handle_multi_page_pdf(
                task_id, file_path, page_images, gemini, openai_client
            )
        ]
    else:
        analyzed_items = [(file_path.name, page_images, receipt_like)]

    return [
        (file_index, item_index, label, images, item_receipt_like)
        for item_index, (label, images, item_receipt_like) in enumerate(analyzed_items)
    ]


async def _produce_work_items(
    task_id: str,
    files: list[Path],
    gemini: GeminiClient,
    openai_client: OpenAIClient,
    work_queue: asyncio.Queue[WorkItem | None],
    results: dict[tuple[int, int], InvoiceFields],
) -> None:
    """Stream analyzed work items into the OCR queue as soon as they are ready."""
    semaphore = asyncio.Semaphore(
        max(1, min(settings.max_concurrency, ANALYSIS_CONCURRENCY_LIMIT))
    )

    async def analyze_limited(file_index: int, file_path: Path) -> AnalysisOutcome:
        async with semaphore:
            try:
                work_items = await _analyze_file(task_id, file_index, file_path, gemini, openai_client)
                return (file_index, file_path.name, work_items, None)
            except Exception as e:
                return (file_index, file_path.name, [], str(e))

    analysis_tasks = [
        asyncio.create_task(analyze_limited(file_index, file_path))
        for file_index, file_path in enumerate(files)
    ]

    try:
        for completed in asyncio.as_completed(analysis_tasks):
            file_index, file_name, work_items, error_message = await completed
            task = task_store.get(task_id)
            if not task or task.status == "cancelled":
                continue

            if error_message is not None:
                logger.warning(
                    f"File analysis failed for {file_name} "
                    f"(recording error row): {error_message}"
                )
                results[(file_index, 0)] = InvoiceFields(
                    file_name=file_name,
                    error=f"Analysis failed: {error_message}",
                )
                task.total_files += 1
                task.processed_files += 1
                if task.status == "analyzing":
                    task.status = "processing"
                task_store.notify(task_id)
                continue

            for file_index, item_index, label, images, receipt_like in work_items:
                task = task_store.get(task_id)
                if not task or task.status == "cancelled":
                    return

                task.total_files += 1
                task.pending_files.append(label)
                if task.status == "analyzing":
                    task.status = "processing"
                task_store.notify(task_id)
                await work_queue.put((file_index, item_index, label, images, receipt_like))
    finally:
        for analysis_task in analysis_tasks:
            if not analysis_task.done():
                analysis_task.cancel()
        if analysis_tasks:
            await asyncio.gather(*analysis_tasks, return_exceptions=True)


async def _process_single_invoice(
    task_id: str,
    file_label: str,
    images: list[bytes],
    receipt_like: bool,
    gemini: GeminiClient,
    openai_client: OpenAIClient,
    claude: ClaudeClient,
) -> InvoiceFields:
    """Process a single invoice: Gemini + OpenAI parallel -> compare -> maybe Claude."""
    extraction_prompt = build_extraction_prompt(receipt_like)
    _track_call(task_id, "gemini", "extract")
    _track_call(task_id, "openai", "extract")
    gemini_result, openai_result = await asyncio.gather(
        _call_with_retry(
            lambda: gemini.extract_invoice(images, file_label, prompt=extraction_prompt)
        ),
        _call_with_retry(
            lambda: openai_client.extract_invoice(images, file_label, prompt=extraction_prompt)
        ),
        return_exceptions=True,
    )
    gemini_error = (
        _compact_exception(gemini_result)
        if isinstance(gemini_result, Exception)
        else None
    )
    openai_error = (
        _compact_exception(openai_result)
        if isinstance(openai_result, Exception)
        else None
    )

    if isinstance(gemini_result, Exception) and isinstance(openai_result, Exception):
        return InvoiceFields(
            file_name=file_label,
            error=f"Both models failed. Gemini: {gemini_error}; OpenAI: {openai_error}",
            gemini_error=gemini_error,
            openai_error=openai_error,
        )

    if isinstance(gemini_result, Exception):
        logger.warning(f"Gemini failed for {file_label}: {gemini_result}")
        if isinstance(openai_result, InvoiceFields):
            result = await _review_single_model_result(
                task_id=task_id,
                file_label=file_label,
                images=images,
                receipt_like=receipt_like,
                claude=claude,
                fallback_result=openai_result,
                fallback_source_prefix="openai-only",
                gemini_error=gemini_error,
            )
            return await _apply_total_reviews(
                task_id=task_id,
                file_label=file_label,
                images=images,
                receipt_like=receipt_like,
                result=result,
                claude=claude,
            )
        return InvoiceFields(
            file_name=file_label,
            error=gemini_error,
            gemini_error=gemini_error,
        )

    if isinstance(openai_result, Exception):
        logger.warning(f"OpenAI failed for {file_label}: {openai_result}")
        if isinstance(gemini_result, InvoiceFields):
            result = await _review_single_model_result(
                task_id=task_id,
                file_label=file_label,
                images=images,
                receipt_like=receipt_like,
                claude=claude,
                fallback_result=gemini_result,
                fallback_source_prefix="gemini-only",
                openai_error=openai_error,
            )
            return await _apply_total_reviews(
                task_id=task_id,
                file_label=file_label,
                images=images,
                receipt_like=receipt_like,
                result=result,
                claude=claude,
            )
        return InvoiceFields(
            file_name=file_label,
            error=openai_error,
            openai_error=openai_error,
        )

    try:
        _track_call(task_id, "claude", "arbitrate")
        result = await _call_with_retry(
            lambda: compare_and_arbitrate(
                gemini_result,
                openai_result,
                images,
                file_label,
                claude,
                receipt_like=receipt_like,
            )
        )
        result = _attach_model_errors(result)
        return await _apply_total_reviews(
            task_id=task_id,
            file_label=file_label,
            images=images,
            receipt_like=receipt_like,
            result=result,
            claude=claude,
        )
    except Exception as e:
        logger.warning(f"Arbitration failed for {file_label}: {e}, using Gemini result")
        gemini_result.source_model = f"gemini-fallback:{gemini_result.source_model}"
        result = _attach_model_errors(gemini_result)
        return await _apply_total_reviews(
            task_id=task_id,
            file_label=file_label,
            images=images,
            receipt_like=receipt_like,
            result=result,
            claude=claude,
        )


async def _ocr_worker(
    task_id: str,
    work_queue: asyncio.Queue[WorkItem | None],
    results: dict[tuple[int, int], InvoiceFields],
    gemini: GeminiClient,
    openai_client: OpenAIClient,
    claude: ClaudeClient,
) -> None:
    """Consume work items from the queue and run OCR as soon as items arrive."""
    while True:
        work_item = await work_queue.get()
        try:
            if work_item is None:
                return

            file_index, item_index, label, images, receipt_like = work_item
            task = await _wait_until_active(task_id)
            if not task:
                continue

            task.current_file = label
            task_store.notify(task_id)

            try:
                result = await _process_single_invoice(
                    task_id, label, images, receipt_like, gemini, openai_client, claude
                )
            except Exception as e:
                result = InvoiceFields(file_name=label, error=str(e))

            # Try to fill missing consumption_tax from tax_verification
            maybe_fill_consumption_tax(result)

            # Post-extraction amount consistency check
            consistency_warnings = check_amount_consistency(result)
            if consistency_warnings:
                warning_text = "; ".join(consistency_warnings)
                logger.warning("Amount consistency issue for %s: %s", label, warning_text)
                if result.error:
                    result.error = f"{result.error}; {warning_text}"
                else:
                    result.error = warning_text

            results[(file_index, item_index)] = result
            if label in task.pending_files:
                task.pending_files.remove(label)
            task.processed_files += 1
            task_store.notify(task_id)
        finally:
            work_queue.task_done()


async def process_task(task_id: str, zip_path: Path, task_dir: Path) -> None:
    """Main entry point: process an uploaded ZIP file."""
    task = task_store.get(task_id)
    if not task:
        return

    try:
        task.status = "extracting"
        task.current_file = "正在解压 ZIP..."
        task_store.notify(task_id)

        files = await asyncio.to_thread(extract_zip, zip_path, task_dir)
        if task.status == "cancelled":
            task.current_file = ""
            task_store.mark_finished(task_id)
            task_store.notify(task_id)
            return

        task.status = "analyzing"
        task.current_file = "正在分析文件..."
        task_store.notify(task_id)

        gemini, openai_client, claude = _get_clients()
        queue_size = max(
            ANALYSIS_CONCURRENCY_LIMIT,
            max(1, settings.max_concurrency) * WORK_QUEUE_MULTIPLIER,
        )
        work_queue: asyncio.Queue[WorkItem | None] = asyncio.Queue(maxsize=queue_size)
        results: dict[tuple[int, int], InvoiceFields] = {}

        worker_count = max(1, settings.max_concurrency)
        workers = [
            asyncio.create_task(
                _ocr_worker(
                    task_id,
                    work_queue,
                    results,
                    gemini,
                    openai_client,
                    claude,
                )
            )
            for _ in range(worker_count)
        ]

        producer_error: Exception | None = None
        try:
            await _produce_work_items(task_id, files, gemini, openai_client, work_queue, results)
        except Exception as e:
            producer_error = e
        finally:
            for _ in workers:
                await work_queue.put(None)

        worker_results = await asyncio.gather(*workers, return_exceptions=True)

        # Log non-fatal errors but don't abort — preserve completed results
        if producer_error is not None:
            logger.error(f"Producer error in task {task_id}: {producer_error}")
        for worker_result in worker_results:
            if isinstance(worker_result, Exception):
                logger.error(f"Worker error in task {task_id}: {worker_result}")

        task.completed_results = [results[key] for key in sorted(results)]

        if task.status != "cancelled" or task.completed_results:
            if task.status != "cancelled":
                task.status = "writing_excel"
            task.current_file = "正在生成 Excel..."
            task_store.notify(task_id)

            excel_path = get_task_output_path(task_id)
            await asyncio.to_thread(write_excel, task.completed_results, excel_path)
            task.excel_ready = True

        if task.status != "cancelled":
            task.status = "done"
        task.current_file = ""
        task_store.mark_finished(task_id)
        task_store.notify(task_id)

    except Exception as e:
        logger.error(f"Task {task_id} failed: {traceback.format_exc()}")
        task.status = "error"
        task.error_message = str(e)
        task_store.mark_finished(task_id)
        task_store.notify(task_id)
