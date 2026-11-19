"""Text extraction from uploaded files. Returns a list of (page_number | None, text)."""

from __future__ import annotations

import io
from dataclasses import dataclass

ALLOWED_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
}
EXTENSIONS = {v: k for k, v in ALLOWED_TYPES.items()}
DOCX_TYPE = EXTENSIONS[".docx"]


class ExtractionError(Exception):
    pass


@dataclass
class Page:
    number: int | None
    text: str


def sniff_content_type(filename: str, data: bytes, declared: str | None) -> str:
    """Decide the type from content and extension, never trusting the browser alone."""
    lower = filename.lower()
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04") and lower.endswith(".docx"):
        return DOCX_TYPE
    for ext in (".txt", ".md"):
        if lower.endswith(ext):
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ExtractionError("Text files must be UTF-8.") from exc
            return EXTENSIONS[ext]
    raise ExtractionError("Unsupported file type. Upload a PDF, Word (.docx), .txt or .md file.")


def extract_pages(data: bytes, content_type: str) -> list[Page]:
    if content_type == "application/pdf":
        return _pdf(data)
    if content_type == DOCX_TYPE:
        return _docx(data)
    if content_type in ("text/plain", "text/markdown"):
        return [Page(None, data.decode("utf-8"))]
    raise ExtractionError(f"Unsupported content type {content_type}")


def _pdf(data: bytes) -> list[Page]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError("Password-protected PDFs can't be indexed.")
        pages = [Page(i + 1, (p.extract_text() or "")) for i, p in enumerate(reader.pages)]
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Couldn't read PDF: {exc}") from exc
    if not any(p.text.strip() for p in pages):
        raise ExtractionError(
            "No text found in this PDF. It may be a scan; run OCR on it before uploading."
        )
    return pages


def _docx(data: bytes) -> list[Page]:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"Couldn't read Word document: {exc}") from exc
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return [Page(None, "\n".join(parts))]
