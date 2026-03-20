import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp", ".heic", ".heif"}
PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS


def extract_zip(zip_path: Path, output_dir: Path) -> list[Path]:
    """Extract ZIP and return list of supported invoice files."""
    extract_dir = output_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    resolved_extract = extract_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            # Reject absolute paths and path-traversal sequences
            member_path = (extract_dir / member.filename).resolve()
            if not str(member_path).startswith(str(resolved_extract) + "/") and member_path != resolved_extract:
                logger.warning(f"Skipping unsafe ZIP entry: {member.filename!r}")
                continue
            zf.extract(member, extract_dir)

    # Recursively collect supported files, skip hidden/system files
    files: list[Path] = []
    for p in sorted(extract_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.name.startswith(".") or p.name.startswith("__"):
            continue
        if p.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(p)

    return files


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() in PDF_EXTENSIONS
