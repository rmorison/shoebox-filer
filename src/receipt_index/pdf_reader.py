"""PDF text extraction with pdfplumber, in a bounded child process.

A PDF with too little text, such as a scan, gives an empty string. The
caller then sends the PDF itself to the extraction agent as a document
block (see ADR-0004), so this module makes no model calls.

The PDF is untrusted, and reading its text decodes its page content: a file
of under a kilobyte can hold pdfplumber for minutes or inflate to gigabytes.
So pdfplumber never runs in the caller's process. :func:`read_pdf` runs it
through PDF validation's runner (:func:`receipt_index.pdf_validate.run_bounded`),
under its own address-space, CPU, wall-clock and size limits, and reads
no text from a PDF over a page cap (:data:`RECEIPT_BOUNDS`). A PDF that hits
a limit or the cap reads as unreadable, the same as one pdfplumber can't open.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import asdict, dataclass

from receipt_index.pdf_validate import RECEIPT_BOUNDS, PdfLimits, run_bounded

logger = logging.getLogger(__name__)

# Minimum non-whitespace characters to consider pdfplumber output valid.
# A minimal receipt must contain at least a vendor name and amount; 20 chars
# provides a buffer while catching truly empty extractions from image-only PDFs.
_MIN_TEXT_LENGTH = 20


@dataclass(frozen=True)
class PdfRead:
    """What pdfplumber found in one PDF. ``text`` is None when not read."""

    pages: int
    text: str | None
    encrypted: bool


def extract_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes with pdfplumber, in a bounded child.

    Args:
        pdf_bytes: Raw PDF file content.

    Returns:
        The extracted text, or an empty string when there is too little
        text to use (fewer than 20 non-whitespace characters), as in a
        scanned or image-only PDF, or when the PDF can't be read, a hit
        limit included.
    """
    read = read_pdf(pdf_bytes)
    text = (read.text or "") if read is not None else ""
    if _is_sufficient_text(text):
        logger.debug("pdfplumber extracted %d chars from PDF", len(text))
        return text

    logger.info(
        "pdfplumber extracted insufficient text (%d non-whitespace chars)",
        len("".join(text.split())),
    )
    return ""


def read_pdf(
    data: bytes,
    *,
    text_up_to_pages: int | None = None,
    limits: PdfLimits = RECEIPT_BOUNDS.read,
    max_pages: int = RECEIPT_BOUNDS.max_read_pages,
) -> PdfRead | None:
    """A PDF's page count, text and encryption, or None when it can't be read.

    pdfplumber runs in a bounded child (see the module docstring); a limit,
    kill or timeout means unreadable, and so does a PDF over ``max_pages``,
    whose text is never read. With ``text_up_to_pages``, a longer PDF's text
    isn't read either, but its page count is returned.
    """
    limit = max_pages if text_up_to_pages is None else min(text_up_to_pages, max_pages)
    output = run_bounded("read", data, limits, str(limit))
    if output is None:
        return None
    try:
        found = json.loads(output)
        read = None if found is None else PdfRead(**found)
    except (ValueError, TypeError):
        logger.warning("PDF read gave no result (%d bytes)", len(data))
        return None
    if read is not None and read.pages > max_pages:
        logger.info(
            "PDF of %d pages is over the %d-page cap; not reading its text",
            read.pages,
            max_pages,
        )
        return None
    return read


def read_job(data: bytes, args: list[str]) -> bytes:
    """The child's ``read`` job: :func:`_read_unbounded`, as JSON."""
    limit = int(args[0])
    read = _read_unbounded(data, text_up_to_pages=limit)
    return json.dumps(None if read is None else asdict(read)).encode()


def _read_unbounded(
    data: bytes, *, text_up_to_pages: int | None = None
) -> PdfRead | None:
    """pdfplumber's read of ``data``. Unbounded: call it only in the child.

    Any failure, on opening or on a page, means unreadable, never a scan.
    """
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = len(pdf.pages)
            encrypted = pdf.doc.encryption is not None
            if text_up_to_pages is not None and pages > text_up_to_pages:
                return PdfRead(pages, None, encrypted)
            texts = []
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
                # pdfplumber keeps each page's layout objects until the file
                # closes; a long document would otherwise outgrow the limit.
                page.close()
    except Exception:  # any failure, a hit memory limit included
        logger.debug("pdfplumber can't read the PDF", exc_info=True)
        return None
    return PdfRead(pages, "\n\n".join(texts).strip(), encrypted)


def _is_sufficient_text(text: str) -> bool:
    """Check if extracted text meets the minimum quality threshold."""
    stripped = "".join(text.split())
    return len(stripped) >= _MIN_TEXT_LENGTH
