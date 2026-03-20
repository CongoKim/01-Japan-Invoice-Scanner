from pathlib import Path
import fitz  # PyMuPDF


def render_pdf_pages(pdf_path: Path, dpi: int = 300) -> list[bytes]:
    """Render each page of a PDF as a PNG image (bytes).

    Returns a list of PNG byte strings, one per page.
    """
    doc = fitz.open(str(pdf_path))
    pages: list[bytes] = []

    zoom = dpi / 72  # 72 is default PDF DPI
    matrix = fitz.Matrix(zoom, zoom)

    for page in doc:
        pix = page.get_pixmap(matrix=matrix)
        pages.append(pix.tobytes("png"))

    doc.close()
    return pages
