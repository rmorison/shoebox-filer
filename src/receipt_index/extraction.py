"""LLM-based metadata extraction using pydantic-ai."""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from typing import Any

from pydantic_ai import Agent, BinaryContent

from receipt_index.models import (
    Attachment,
    ExtractionResult,
    RawReceipt,
    ReceiptMetadata,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a receipt metadata extractor. Given an email that contains or forwards \
a receipt, extract the following fields:

- vendor: The canonical business name (e.g. "Amazon", not "no-reply@amazon.com")
- amount: The total amount charged (numeric, e.g. 42.99)
- currency: ISO 4217 currency code (e.g. "USD", "CAD", "EUR")
- date: The purchase/transaction date (YYYY-MM-DD), NOT the email send date
- description: Brief summary of what was purchased (optional)
- confidence: Your confidence in the extraction from 0.0 to 1.0. \
Use below 0.5 if the email may not be a receipt or key fields are uncertain.

If PDF attachment content is provided below the email body, use it as additional \
context for extraction. The PDF content often contains the detailed receipt with \
amounts and line items that may not appear in the email body.

If image attachments are provided, they are likely a screenshot or photo of the \
receipt itself (the email body may be empty) — extract the receipt details from \
the image.

Payment confirmations (Zelle, Venmo, PayPal, bank transfers, wire transfers) are \
valid receipts; the vendor is the payment recipient. Use high confidence when the \
amount, recipient, and date are clearly visible.

Handle forwarded receipts by looking at the original receipt content. \
For multi-item orders, use the total amount. If the currency is not stated, \
assume USD.\
"""

DOCUMENT_SYSTEM_PROMPT = """\
You are a receipt metadata extractor. Given a receipt document (scanned image \
or PDF), extract the following fields:

- vendor: The business name on the receipt (or payment recipient)
- amount: The total amount charged (numeric, e.g. 42.99). May be 0 for \
prepaid items (e.g. prepaid postage).
- currency: ISO 4217 currency code (e.g. "USD")
- date: The transaction date (YYYY-MM-DD)
- description: Brief summary of what was purchased (optional)
- confidence: Your confidence in the extraction from 0.0 to 1.0. \
Use below 0.5 if this does not appear to be a receipt or key fields are unclear.

Payment confirmations (Zelle, Venmo, PayPal, bank transfers, wire transfers) \
are valid receipts. The vendor is the payment recipient. Use high confidence \
if the amount, recipient, and date are clearly visible.

For multi-item receipts, use the total amount. If the currency is not stated, \
assume USD.\
"""

# Minimum non-whitespace characters for PDF text to be considered sufficient.
# Matches the threshold used in pdf_reader.py.
_MIN_TEXT_LENGTH = 20


def _to_extraction_result(result: Any) -> ExtractionResult:
    """Convert a pydantic-ai AgentRunResult to an ExtractionResult."""
    usage = result.usage()
    return ExtractionResult(
        metadata=result.output,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=usage.cache_read_tokens or 0,
        requests=usage.requests or 0,
    )


_IMAGE_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def create_extraction_agent(
    *,
    api_key: str,
    model: str,
    system_prompt: str = _SYSTEM_PROMPT,
) -> Agent[None, ReceiptMetadata]:
    """Create a pydantic-ai Agent configured for receipt extraction."""
    return Agent(
        f"anthropic:{model}",
        output_type=ReceiptMetadata,
        system_prompt=system_prompt,
    )


def extract_metadata(
    raw: RawReceipt,
    *,
    agent: Agent[None, ReceiptMetadata] | None = None,
) -> ExtractionResult:
    """Extract structured metadata from a raw receipt.

    Routes to the appropriate extraction path based on ``raw.source_type``:
    - ``"gdrive"``: document-based extraction (vision or PDF text)
    - Otherwise: email-based extraction (existing path)

    Returns an ExtractionResult with metadata and LLM usage info.
    """
    if raw.source_type == "gdrive":
        return _extract_from_document(raw, agent=agent)
    return _extract_from_email(raw, agent=agent)


def _extract_from_email(
    raw: RawReceipt,
    *,
    agent: Agent[None, ReceiptMetadata] | None = None,
) -> ExtractionResult:
    """Extract metadata from an email-sourced receipt."""
    if agent is None:
        msg = "No extraction agent provided."
        raise ValueError(msg)

    pdf_text = _extract_pdf_text(raw.attachments)
    prompt = _build_prompt(raw, pdf_text=pdf_text)

    images = _image_attachments(raw.attachments)
    if images:
        # Send image attachments (e.g. screenshot receipts) to vision alongside
        # the email text context. The IMAP body is often empty for these.
        message: list[Any] = [prompt]
        for att in images:
            message.append(
                BinaryContent(
                    data=att.data,
                    media_type=_base_content_type(att.content_type),
                )
            )
        result: Any = agent.run_sync(message)
    else:
        result = agent.run_sync(prompt)
    return _to_extraction_result(result)


def _extract_from_document(
    raw: RawReceipt,
    *,
    agent: Agent[None, ReceiptMetadata] | None = None,
) -> ExtractionResult:
    """Extract metadata from a Drive-sourced document (PDF or image).

    For PDFs: extract text first; if insufficient, fall back to vision.
    For images: send directly to the LLM as image input.
    """
    if agent is None:
        msg = "No extraction agent provided."
        raise ValueError(msg)

    if raw.file_content is None:
        raise ValueError("Drive-sourced receipt has no file_content")

    content_type = _base_content_type(raw.file_content_type or "")

    if content_type == "application/pdf":
        return _extract_from_pdf_document(raw.file_content, agent=agent)

    if content_type in _IMAGE_CONTENT_TYPES:
        return _extract_from_image(raw.file_content, content_type, agent=agent)

    raise ValueError(
        f"Unsupported file content type for document extraction: {content_type!r}"
    )


def _extract_from_pdf_document(
    pdf_bytes: bytes,
    *,
    agent: Agent[None, ReceiptMetadata],
) -> ExtractionResult:
    """Extract metadata from a PDF document.

    Tries text extraction first. If the text is insufficient (< 20 non-whitespace
    chars), falls back to sending the PDF as binary content for vision analysis.
    """
    from receipt_index.pdf_reader import extract_text

    text = extract_text(pdf_bytes)

    if _has_sufficient_text(text):
        logger.debug("Using text-based extraction for PDF (%d chars)", len(text))
        result: Any = agent.run_sync(
            f"Extract receipt metadata from this document text:\n\n{text}"
        )
        return _to_extraction_result(result)

    logger.info(
        "PDF text extraction insufficient (%d non-ws chars), using vision",
        len("".join(text.split())),
    )
    result = agent.run_sync(
        [
            "Extract receipt metadata from this PDF document.",
            BinaryContent(data=pdf_bytes, media_type="application/pdf"),
        ]
    )
    return _to_extraction_result(result)


def _extract_from_image(
    image_data: bytes,
    content_type: str,
    *,
    agent: Agent[None, ReceiptMetadata],
) -> ExtractionResult:
    """Extract metadata from an image file by sending it to the LLM via vision."""
    result: Any = agent.run_sync(
        [
            "Extract receipt metadata from this receipt image.",
            BinaryContent(data=image_data, media_type=content_type),
        ]
    )
    return _to_extraction_result(result)


def _has_sufficient_text(text: str) -> bool:
    """Check if extracted text meets the minimum quality threshold."""
    stripped = "".join(text.split())
    return len(stripped) >= _MIN_TEXT_LENGTH


def _build_prompt(raw: RawReceipt, *, pdf_text: str | None = None) -> str:
    """Build the user prompt from raw receipt data."""
    parts = [
        f"Subject: {raw.subject}",
        f"From: {raw.sender}",
        f"Date: {raw.date.isoformat()}",
        "",
        "--- Email Body ---",
    ]

    if raw.text_body:
        parts.append(raw.text_body)
    elif raw.html_body:
        parts.append(_strip_html_tags(raw.html_body))
    else:
        parts.append("(no body content)")

    if pdf_text:
        parts.append("")
        parts.append("--- PDF Attachment Content ---")
        parts.append(pdf_text)

    return "\n".join(parts)


def _extract_pdf_text(attachments: list[Attachment]) -> str | None:
    """Extract text from the first PDF attachment, if any."""
    from receipt_index.pdf_reader import extract_text

    for att in attachments:
        if _base_content_type(att.content_type) == "application/pdf":
            text = extract_text(att.data)
            return text if text else None
    return None


def _base_content_type(content_type: str) -> str:
    """Normalize a MIME type to its lowercase base (no parameters)."""
    return content_type.split(";")[0].strip().lower()


def _image_attachments(attachments: list[Attachment]) -> list[Attachment]:
    """Return attachments whose content type is a supported image."""
    return [
        att
        for att in attachments
        if _base_content_type(att.content_type) in _IMAGE_CONTENT_TYPES
    ]


def _strip_html_tags(html: str) -> str:
    """Remove HTML tags, returning only text content."""
    stripper = _HTMLTagStripper()
    stripper.feed(html)
    return stripper.get_text()


class _HTMLTagStripper(HTMLParser):
    """HTMLParser subclass that strips tags and returns text."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._parts.append(f"&#{name};")

    def get_text(self) -> str:
        return "".join(self._parts)
