"""LLM-based metadata extraction using pydantic-ai."""

from __future__ import annotations

import functools
import io
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from pydantic_ai import Agent, BinaryContent
from pydantic_ai.capabilities import RaiseContentFilterError
from pydantic_ai.exceptions import ContentFilterError

from receipt_index.llm import anthropic_model
from receipt_index.models import (
    Attachment,
    ExtractionResult,
    RawReceipt,
    ReceiptMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")

# Raised whenever the prompts or the output model change; every extraction
# run records it. 0 means a receipt has no extraction history; 1 was the
# paid-amount prompt, which was never recorded. 3 added receipt_link, and
# likely receipt link URLs in the text of an HTML-only email. 4 sends a
# scanned PDF attachment (no text layer) to the model as a document. 5 added
# order_number and payment, with their field confidences. 6 added kind, and
# asks a refund or credit for the original order number it refers to.
EXTRACTION_PROMPT_VERSION = 6

# The amount, paid-amount and field-confidence rules, shared by both prompts
# so that they can't drift apart. See the receipt data-model design, §1 and §2.
_AMOUNT_RULES = """\
- amount: The order, invoice or statement total as the document states it \
(numeric, e.g. 42.99). May be 0 for prepaid items (e.g. prepaid postage).
- paid_amount: What this transaction charged to the payer's payment methods \
(card, bank account, wallet or cash), or for a refund, what was returned to \
them. Rules:
  - Include tax, shipping and tip; it is after discounts and coupons.
  - Exclude anything paid with gift cards, store credit, points or vouchers; \
they never reach a bank or card statement. When they were used, say in the \
description how much and with what.
  - For an invoice or statement that shows a payment, use the payment, not \
the balance or the invoice total.
  - For an order charged in parts (for example on each shipment), use what \
this document says was charged.
  - A pre-authorization or hold is not a payment.
  - When the document shows that nothing has been paid (an unpaid invoice), \
use 0.
  - For a refund, what was returned to the payer's payment methods, excluding \
any part returned as store credit, a gift card or points. It is never \
negative: use 0 when all of it was returned as store credit, a gift card or \
points.
  - For a purchase, when the document shows a payment (a card or account \
line with an amount, "paid", "amount charged"), give it, even when it equals \
the total. For \
cash, it is the amount charged, net of change, not the cash tendered. A \
payment method named with no amount (a card on file, a guarantee, "payment \
method: card ending 1234") does not show what was paid. Don't assume the \
total was paid when no payment is shown. When the document does not say what \
was paid or returned, use null: null means not known, and 0 means nothing was \
charged.
"""

_FIELD_CONFIDENCE_RULE = """\
- field_confidence: Your confidence in each of vendor, amount, paid_amount and \
date, from 0.0 to 1.0. Each is always a number, even when paid_amount is null \
(then it is your confidence that the document does not say what was paid). Go \
below 0.5 when a figure is inferred rather than printed on the document. For \
order_number, payment and kind: your confidence in each, or null when the \
field is null.
"""

# The order number and payment fields, shared by both prompts. See the receipt
# data-model design, §4. Only the last four digits of a card are ever kept.
_ORDER_PAYMENT_RULES = """\
- order_number: The order, confirmation or invoice number as printed, without \
its label (for "Order #A-100" give "A-100"). When several are shown, the order \
number. For a refund or credit, the original order or invoice number it \
refers to when the document shows one, rather than the refund's own reference \
number. Use null when there is none.
- payment: How this document says it was paid, or null when it shows no \
payment details. A card named with no amount still counts here. Its fields:
  - method: one of card, bank, wallet, cash, transfer or other (null when \
not shown).
  - last4: only the last four digits of the card or account number, even when \
the document shows more (null when not shown).
  - payer: the cardholder or bill-to name as printed (null when not shown), \
not the person who sent or forwarded the email.
Never write more than the last four digits of a card or account number \
anywhere in your output, the description included.
"""

# What the document is, which sets the sign of its amount in a total. Shared
# by both prompts. See the receipt data-model design, §6.
_KIND_RULES = """\
- kind: one of purchase, refund, credit or statement. Rules:
  - purchase: a charge or payment for goods or services: a receipt, an order \
confirmation, an invoice or bill (one for a single service period included), \
or a payment confirmation.
  - refund: money returned for an earlier purchase: a return, a cancellation, \
a reversal or a chargeback.
  - credit: a credit that is not money returned for one purchase: a credit \
memo, an account or statement credit, a goodwill credit or a rebate.
  - statement: a periodic statement whose transactions are charged or \
receipted elsewhere: card and bank statements, and vendor statements that \
summarise orders already receipted. A statement that bills a new charge for \
its own period (a utility, phone or subscription statement) is a purchase.
  When you are unsure, use purchase with a low kind confidence. amount and \
paid_amount are never negative, whatever the kind.
"""

# A receipt that is only a link to a hosted receipt. Shared by both prompts.
_RECEIPT_LINK_RULE = """\
- receipt_link: When the receipt's details (the amounts, the items) are not \
in the document and are only behind a link to a hosted receipt (for example \
"View your receipt"), give that link's full URL exactly as written, and use \
a confidence below 0.5. Still give vendor, amount (0 when no amount is \
shown) and date (the date of the document when no purchase date is shown). \
Use null when the receipt's details are in the \
document, its text or its attachments, even if it also has a link to view \
them online. Use null too when there is no such link.
"""

_SYSTEM_PROMPT = f"""\
You are a receipt metadata extractor. Given an email that contains or forwards \
a receipt, extract the following fields:

- vendor: The canonical business name (e.g. "Amazon", not "no-reply@amazon.com")
{_AMOUNT_RULES}\
- currency: ISO 4217 currency code (e.g. "USD", "CAD", "EUR")
- date: The purchase/transaction date (YYYY-MM-DD), NOT the email send date
- description: Brief summary of what was purchased (optional)
{_ORDER_PAYMENT_RULES}\
{_KIND_RULES}\
- confidence: Your confidence in the extraction from 0.0 to 1.0. \
Use below 0.5 if the email may not be a receipt or key fields are uncertain.
{_FIELD_CONFIDENCE_RULE}\
- receipt_document: Which part of the email holds the receipt: the file name \
of the attachment exactly as listed under "Attachments", or "body" when the \
receipt is in the email text itself. Use null when you can't tell, for \
example when the only attachment holds terms and conditions.
{_RECEIPT_LINK_RULE}\

If PDF attachment content is provided below the email body, use it as additional \
context for extraction. The PDF content often contains the detailed receipt with \
amounts and line items that may not appear in the email body.

A PDF attachment with no text layer (a scan) is sent as a document after a line \
"--- PDF Attachment (scanned): <file name> ---". Read the receipt from it, and \
when it holds the receipt, give that file name as receipt_document.

If image attachments are provided, they are likely a screenshot or photo of the \
receipt itself (the email body may be empty) — extract the receipt details from \
the image.

Payment confirmations (Zelle, Venmo, PayPal, bank transfers, wire transfers) are \
valid receipts; the vendor is the payment recipient. Use high confidence when the \
amount, recipient, and date are clearly visible.

Handle forwarded receipts by looking at the original receipt content. \
If the currency is not stated, assume USD.\
"""

DOCUMENT_SYSTEM_PROMPT = f"""\
You are a receipt metadata extractor. Given a receipt document (scanned image \
or PDF), extract the following fields:

- vendor: The business name on the receipt (or payment recipient)
{_AMOUNT_RULES}\
- currency: ISO 4217 currency code (e.g. "USD")
- date: The transaction date (YYYY-MM-DD)
- description: Brief summary of what was purchased (optional)
{_ORDER_PAYMENT_RULES}\
{_KIND_RULES}\
- confidence: Your confidence in the extraction from 0.0 to 1.0. \
Use below 0.5 if this does not appear to be a receipt or key fields are unclear.
{_FIELD_CONFIDENCE_RULE}\
{_RECEIPT_LINK_RULE}
Payment confirmations (Zelle, Venmo, PayPal, bank transfers, wire transfers) \
are valid receipts. The vendor is the payment recipient. Use high confidence \
if the amount, recipient, and date are clearly visible.

If the currency is not stated, assume USD.\
"""

# Minimum non-whitespace characters for PDF text to be considered sufficient.
# Matches the threshold used in pdf_reader.py.
_MIN_TEXT_LENGTH = 20

# Scanned PDF attachments sent to the model as documents, per email. Every
# page costs input tokens for its text and its image, so these bound one
# email's cost: a scanned receipt or invoice runs to a page or three, and a
# longer scan is unlikely to be a receipt. The byte cap, which image
# attachments count toward too, keeps the request, after base64, under the
# Messages API's 32 MB limit.
_MAX_EMAIL_PDF_DOCUMENTS = 3
_MAX_EMAIL_PDF_PAGES = 10
_MAX_EMAIL_PDF_BYTES = 20_000_000


def _to_extraction_result(result: Any) -> ExtractionResult:
    """Convert a pydantic-ai AgentRunResult to an ExtractionResult."""
    usage = result.usage
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
    llm, settings = anthropic_model(api_key=api_key, model=model)
    return Agent(
        llm,
        output_type=ReceiptMetadata,
        system_prompt=system_prompt,
        model_settings=settings,
        # A refusal ends the run, even after partial text.
        capabilities=[RaiseContentFilterError()],
    )


class ExtractionRefusedError(Exception):
    """The model declined the request (``stop_reason: "refusal"``).

    Haiku 5.5's safety classifiers can decline a request, and it has no
    server-side fallback. Asking again repeats the same request, so this is
    an item failure, not a transient one. Raised from the pydantic-ai
    ``ContentFilterError``.
    """

    def __init__(self, category: str | None, explanation: str | None) -> None:
        self.category = category
        self.explanation = explanation
        msg = "The model declined to extract this receipt"
        if category:
            msg += f" (category: {category})"
        super().__init__(f"{msg}: {explanation}" if explanation else f"{msg}.")


def _refusal_details(exc: ContentFilterError) -> tuple[str | None, str | None]:
    """The refusal's category and explanation, from the refused response."""
    try:
        details = json.loads(exc.body or "[]")[-1].get("provider_details") or {}
    except (ValueError, IndexError, AttributeError):
        details = {}
    return details.get("refusal_category"), details.get("refusal")


def _refusals_as_item_errors(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Turn a model refusal into an ``ExtractionRefusedError``."""

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return func(*args, **kwargs)
        except ContentFilterError as exc:
            raise ExtractionRefusedError(*_refusal_details(exc)) from exc

    return wrapper


@_refusals_as_item_errors
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

    pdfs = _read_pdf_attachments(raw.attachments)
    prompt = _build_prompt(raw, pdf_text=pdfs.text, pdf_name=pdfs.text_name)

    images = _image_attachments(raw.attachments)
    if not pdfs.documents and not images:
        return _to_extraction_result(agent.run_sync(prompt))

    # Scanned PDFs go as documents, each after a line naming its file, and
    # image attachments (e.g. screenshot receipts) as images, in the same
    # request as the email text. The IMAP body is often empty for these.
    message: list[Any] = [prompt]
    for att in pdfs.documents:
        message.append(f"--- PDF Attachment (scanned): {_label_name(att.filename)} ---")
        message.append(BinaryContent(data=att.data, media_type="application/pdf"))
    for att in images:
        message.append(
            BinaryContent(
                data=att.data,
                media_type=_base_content_type(att.content_type),
            )
        )
    result: Any = agent.run_sync(message)
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


@_refusals_as_item_errors
def extract_from_rendition(
    pdf_bytes: bytes,
    *,
    subject: str,
    sender: str,
    agent: Agent[None, ReceiptMetadata],
) -> ExtractionResult:
    """Extract metadata from a stored rendition (the receipt's PDF).

    The same text-first, vision-fallback path as a Drive PDF, with the
    stored subject and sender put first, since the rendition of an email
    with a PDF attachment is that attachment alone.
    """
    from receipt_index.pdf_reader import extract_text

    header = f"Subject: {subject}\nFrom: {sender}"
    text = extract_text(pdf_bytes)
    if _has_sufficient_text(text):
        result: Any = agent.run_sync(
            "Extract receipt metadata from this stored receipt document.\n\n"
            f"{header}\n\n--- Document text ---\n{text}"
        )
        return _to_extraction_result(result)

    logger.info("Rendition text insufficient, using vision")
    result = agent.run_sync(
        [
            f"Extract receipt metadata from this stored receipt document.\n\n{header}",
            BinaryContent(data=pdf_bytes, media_type="application/pdf"),
        ]
    )
    return _to_extraction_result(result)


def _has_sufficient_text(text: str) -> bool:
    """Check if extracted text meets the minimum quality threshold."""
    stripped = "".join(text.split())
    return len(stripped) >= _MIN_TEXT_LENGTH


def _build_prompt(
    raw: RawReceipt, *, pdf_text: str | None = None, pdf_name: str | None = None
) -> str:
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

    if raw.attachments:
        # Listed by name so the extractor can say which one holds the receipt.
        parts.append("")
        parts.append("--- Attachments ---")
        for att in raw.attachments:
            name = _label_name(att.filename)
            parts.append(f"- {name} ({_base_content_type(att.content_type)})")

    if pdf_text:
        parts.append("")
        heading = "--- PDF Attachment Content"
        if pdf_name:
            parts.append(f"{heading}: {_label_name(pdf_name)} ---")
        else:
            parts.append(f"{heading} ---")
        parts.append(pdf_text)

    return "\n".join(parts)


@dataclass
class _PdfAttachments:
    """An email's PDF attachments, as the extractor gets them.

    ``text`` is the first PDF with text, by name; ``documents`` are the
    scanned PDFs (no text layer) sent to the model as they are.
    """

    text_name: str | None = None
    text: str | None = None
    documents: list[Attachment] = field(default_factory=list)


@dataclass
class _PdfRead:
    """What pdfplumber found in one PDF. ``text`` is None when not read."""

    pages: int
    text: str | None
    encrypted: bool


def _read_pdf(data: bytes, *, text_up_to_pages: int | None = None) -> _PdfRead | None:
    """A PDF's page count, text and encryption, or None when it can't be read.

    With ``text_up_to_pages``, a longer PDF's text isn't read. Any failure,
    on opening or on a page, means unreadable, never a scan.
    """
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = len(pdf.pages)
            encrypted = pdf.doc.encryption is not None
            if text_up_to_pages is not None and pages > text_up_to_pages:
                return _PdfRead(pages, None, encrypted)
            texts = [page.extract_text() or "" for page in pdf.pages]
    except Exception:  # any failure means unreadable, never a scan
        return None
    return _PdfRead(pages, "\n\n".join(texts).strip(), encrypted)


def _read_pdf_attachments(attachments: list[Attachment]) -> _PdfAttachments:
    """Sort the PDF attachments into text, documents and left out.

    The first PDF with text gives the text section; later ones are left
    out, as before. A PDF that opens but has too little text is a scan and
    goes as a document, within the caps above; image attachments, which
    are all sent, count toward the byte cap. A PDF that can't be read, or
    an encrypted scan, is left out: sent, it would fail the whole request.
    """
    pdfs = _PdfAttachments()
    sent_bytes = sum(len(att.data) for att in _image_attachments(attachments))
    for att in attachments:
        if _base_content_type(att.content_type) != "application/pdf":
            continue
        # Once the text is found, a PDF too long to be a document isn't
        # read through: it can be neither.
        limit = _MAX_EMAIL_PDF_PAGES if pdfs.text is not None else None
        read = _read_pdf(att.data, text_up_to_pages=limit)
        if read is None:
            logger.info(
                "PDF attachment %s can't be read; left out", _label_name(att.filename)
            )
            continue
        if read.text is None:
            reason = f"{read.pages} pages, over {_MAX_EMAIL_PDF_PAGES}"
        elif _has_sufficient_text(read.text):
            if pdfs.text is None:
                pdfs.text_name, pdfs.text = att.filename, read.text
            continue
        elif read.encrypted:
            reason = "encrypted"
        elif len(pdfs.documents) >= _MAX_EMAIL_PDF_DOCUMENTS:
            reason = f"already {_MAX_EMAIL_PDF_DOCUMENTS} scanned PDFs"
        elif read.pages > _MAX_EMAIL_PDF_PAGES:
            reason = f"{read.pages} pages, over {_MAX_EMAIL_PDF_PAGES}"
        elif sent_bytes + len(att.data) > _MAX_EMAIL_PDF_BYTES:
            reason = f"{len(att.data)} bytes, over the email's limit"
        else:
            pdfs.documents.append(att)
            sent_bytes += len(att.data)
            continue
        logger.info("PDF attachment %s left out: %s", _label_name(att.filename), reason)
    return pdfs


_MAX_LABEL_NAME = 255


def _label_name(filename: str) -> str:
    """An attachment's file name, safe to put in a prompt label or a log line.

    The name comes from the email, so it can't be allowed to end its label
    and fake a section: control and line-separator characters (newlines
    among them) become spaces, a run of three or more dashes becomes one,
    and the name is cut to 255 characters. An ordinary name is unchanged,
    so the model gives it back as receipt_document exactly as the MIME
    headers have it; a name that had to be cleaned won't match.
    """
    chars = (
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch
        for ch in filename
    )
    name = re.sub(r"-{3,}", "-", "".join(chars)).strip()
    if len(name) > _MAX_LABEL_NAME:
        name = name[: _MAX_LABEL_NAME - 1].rstrip() + "…"
    return name or "(no name)"


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


# A link worth showing the extractor: its text (an image button's alt text
# included) or URL names a receipt or an order. A best guess: most of a
# newsletter's tracking links stay out of the prompt, though a "View this
# email in your browser" link matches, and receipt_link stays a likely
# receipt.
_RECEIPT_LINK_WORDS = re.compile(
    r"receipt|order|invoice|transaction|purchase|view", re.IGNORECASE
)
# The most link URLs kept from one email.
_MAX_LINKS_KEPT = 10


class _HTMLTagStripper(HTMLParser):
    """HTMLParser subclass that strips tags and returns text.

    The http(s) URL of a link whose text or URL suggests a receipt or an
    order is kept after its text ("View your receipt <https://...>"), up to
    ten per email, so the extractor can see where a receipt that is only a
    link lives.
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        # The open anchor's URL and where its text starts in _parts.
        self._href: str | None = None
        self._href_start = 0
        self._alt: list[str] = []
        self._kept = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img" and self._href is not None:
            # An image button's alt text is its text, for matching only.
            self._alt.append(dict(attrs).get("alt") or "")
            return
        if tag != "a":
            return
        href = (dict(attrs).get("href") or "").strip()
        if href.lower().startswith(("http://", "https://")):
            self._href = href
            self._href_start = len(self._parts)
            self._alt = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        href, self._href = self._href, None
        text = "".join(self._parts[self._href_start :]).strip()
        alt = " ".join(self._alt)
        if self._kept >= _MAX_LINKS_KEPT or not _RECEIPT_LINK_WORDS.search(
            f"{text} {alt} {href}"
        ):
            return
        self._kept += 1
        if text != href:
            self._parts.append(f" <{href}>")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._parts.append(f"&#{name};")

    def get_text(self) -> str:
        return "".join(self._parts)
