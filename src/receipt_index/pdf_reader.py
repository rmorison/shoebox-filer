"""PDF text extraction with pdfplumber.

A PDF with too little text, such as a scan, gives an empty string. The
caller then sends the PDF itself to the extraction agent as a document
block (see ADR-0004), so this module makes no model calls.
"""

from __future__ import annotations

import io
import logging

import pdfplumber

logger = logging.getLogger(__name__)

# Minimum non-whitespace characters to consider pdfplumber output valid.
# A minimal receipt must contain at least a vendor name and amount; 20 chars
# provides a buffer while catching truly empty extractions from image-only PDFs.
_MIN_TEXT_LENGTH = 20


def extract_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes with pdfplumber.

    Args:
        pdf_bytes: Raw PDF file content.

    Returns:
        The extracted text, or an empty string when there is too little
        text to use (fewer than 20 non-whitespace characters), as in a
        scanned or image-only PDF, or when pdfplumber can't read the file.
    """
    text = _extract_with_pdfplumber(pdf_bytes)
    if _is_sufficient_text(text):
        logger.debug("pdfplumber extracted %d chars from PDF", len(text))
        return text

    logger.info(
        "pdfplumber extracted insufficient text (%d non-whitespace chars)",
        len("".join(text.split())),
    )
    return ""


def _extract_with_pdfplumber(pdf_bytes: bytes) -> str:
    """Extract text from PDF using pdfplumber."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n\n".join(pages).strip()
    except Exception:
        logger.warning("pdfplumber failed to process PDF", exc_info=True)
        return ""


def _is_sufficient_text(text: str) -> bool:
    """Check if extracted text meets the minimum quality threshold."""
    stripped = "".join(text.split())
    return len(stripped) >= _MIN_TEXT_LENGTH
