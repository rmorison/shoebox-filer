"""Domain and extraction models for receipt indexing."""

from __future__ import annotations

import re
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    ModelWrapValidatorHandler,
    computed_field,
    field_validator,
    model_validator,
)


@dataclass
class Attachment:
    """An email attachment."""

    filename: str
    content_type: str
    data: bytes


@dataclass
class RawReceipt:
    """Raw receipt data from a source adapter.

    Supports both email-based (IMAP) and file-based (Google Drive) sources.
    Email fields (subject, sender, html_body, text_body, attachments) are used
    by the IMAP adapter. File fields (file_name, file_content, file_content_type)
    are used by the Drive adapter.
    """

    source_id: str
    source_name: str
    source_type: str
    date: datetime
    subject: str = ""
    sender: str = ""
    html_body: str | None = None
    text_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    file_name: str | None = None
    file_content: bytes | None = None
    file_content_type: str | None = None


@dataclass
class ItemRef:
    """One item a source listed: enough to identify it without its body.

    IMAP refs come from a header-only fetch; Drive refs from file metadata.
    ``skip_reason`` marks an item the pipeline records as skipped without
    fetching it (a Drive file of an unsupported type).
    """

    source_id: str
    source_type: str
    subject: str = ""
    sender: str = ""
    date: datetime | None = None
    imap_uid: int | None = None
    drive_meta: dict[str, Any] | None = None
    file_name: str | None = None
    skip_reason: str | None = None
    # Set when the source refused even this item's headers: fetching it
    # fails as an item error, so it gets a failed row and the cursor passes.
    unreadable: str | None = None


@dataclass
class SourceRead:
    """What one read of a source returned: its items and cursor candidates.

    ``reset`` names why a stored cursor was discarded ("uidvalidity changed",
    "invalid page token", "account changed"); a source with no stored cursor
    yet is a first run, not a reset.
    """

    items: list[ItemRef]
    reset: str | None = None
    # IMAP: the folder's UIDVALIDITY, the UID the search started above, and
    # every UID the search returned (a UID with no ref holds the cursor).
    uidvalidity: int | None = None
    start_uid: int = 0
    searched_uids: list[int] = field(default_factory=list)
    # Drive: the token to save once every item is handled, and its account.
    drive_page_token: str | None = None
    drive_account_id: str | None = None
    # Drive: something was left for a later read (settle time), so the
    # token must not move.
    held_back: bool = False


class SyncState(BaseModel):
    """A source's stored read position (``receipt.sync_state``)."""

    id: UUID
    source_name: str
    source_type: str
    folder: str
    imap_uidvalidity: int | None = None
    imap_last_uid: int | None = None
    drive_page_token: str | None = None
    drive_account_id: str | None = None
    archive_enabled_at: datetime | None = None
    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    resets: int = 0
    created_at: datetime
    updated_at: datetime


class FieldConfidence(BaseModel):
    """The model's own confidence in each extracted field, from 0 to 1.

    An estimate, not a measured probability: good for ordering a review,
    not for a hard threshold.
    """

    vendor: float = Field(ge=0.0, le=1.0)
    amount: float = Field(ge=0.0, le=1.0)
    paid_amount: float = Field(ge=0.0, le=1.0)
    date: float = Field(ge=0.0, le=1.0)


class ReceiptMetadata(BaseModel):
    """Structured metadata extracted from a receipt by the LLM."""

    vendor: str = Field(min_length=1)
    # The order, invoice or statement total as the document states it.
    amount: Decimal = Field(ge=0)
    # What this transaction charged to the payer's payment methods (or, for a
    # refund, returned to them). None means not known; never filled from amount.
    paid_amount: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    date: date
    description: str | None = None
    # Which part of an email held the receipt: an attachment's file name as
    # listed, or "body". None when the extractor can't tell, and always None
    # for a Drive document or a rendition.
    receipt_document: str | None = None
    # Whether this is a receipt at all; below 0.5 the item is skipped.
    confidence: float = Field(ge=0.0, le=1.0)
    field_confidence: FieldConfidence
    # The hosted receipt's URL when the receipt's details are only behind a
    # link. A low-confidence item with a link is recorded as a link-only skip.
    # Anything but an http(s) URL of up to 2048 characters becomes None, so a
    # bad link can't fail the extraction. Left out of model_dump, so the
    # append-only extraction history never holds a token it can't drop.
    receipt_link: str | None = Field(default=None, exclude=True)

    @field_validator("receipt_link", mode="before")
    @classmethod
    def _http_link_or_none(cls, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        link = value.strip()
        if len(link) > MAX_RECEIPT_LINK_LENGTH:
            return None
        try:
            parts = urlsplit(link)
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        return link

    @model_validator(mode="wrap")
    @classmethod
    def _link_kept_out_of_errors(
        cls, data: Any, handler: ModelWrapValidatorHandler[ReceiptMetadata]
    ) -> ReceiptMetadata:
        # A validation error echoes its input, truncated in the middle, into
        # the traceback the pipeline logs; a token in the link's path would
        # survive that. So the other fields are validated without the link.
        if not isinstance(data, dict) or "receipt_link" not in data:
            return handler(data)
        rest = {k: v for k, v in data.items() if k != "receipt_link"}
        metadata = handler(rest)
        metadata.receipt_link = cls._http_link_or_none(data["receipt_link"])
        return metadata


# The longest receipt link kept; the ingest_log column has the same limit.
MAX_RECEIPT_LINK_LENGTH = 2048


def link_host(url: str) -> str:
    """A link's host name alone: safe to log, unlike its path and query."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    return host or "unknown host"


_URL = re.compile(r"https?://[^\s'\"<>\\]+", re.IGNORECASE)


def redact_links(text: str) -> str:
    """Every http(s) URL in ``text`` as its host alone."""
    return _URL.sub(lambda m: f"<link: {link_host(m.group(0))}>", text)


def redacted_traceback(exc: BaseException) -> str:
    """``exc``'s traceback, its cause chain included, with links redacted.

    A failed extraction's chain can hold the model's output, or the email
    text sent to it, and so a link whose path or query may carry a personal
    token. Log this instead of ``exc_info=True`` wherever that can be in the
    chain. It catches whole URLs only: pydantic truncates a long input in
    the middle, dropping the scheme, so ReceiptMetadata keeps receipt_link
    out of its validation errors itself.
    """
    return redact_links("".join(traceback.format_exception(exc)))


# Below this overall confidence an extraction isn't treated as a receipt:
# ingest skips the item, and re-extraction records the run and applies
# nothing (it is "gated"). One value for ingest, reextract and --needs-review.
MIN_RECEIPT_CONFIDENCE = 0.5

# An extraction run that failed records only this key in its output. Such a
# run never makes a receipt current, and --needs-review lists it.
FAILED_RUN_KEY = "error"


def receipt_values(metadata: ReceiptMetadata) -> dict[str, Any]:
    """The receipt fields an extraction gives, keyed by their applied name.

    Columns by name (``receipt_date`` for the model's ``date``), and each
    field confidence as ``field_confidence.<field>``: the names that
    ``extractions.applied_fields`` and ``receipts.manual_fields`` use.
    """
    values: dict[str, Any] = {
        "vendor": metadata.vendor,
        "amount": metadata.amount,
        "paid_amount": metadata.paid_amount,
        "currency": metadata.currency,
        "receipt_date": metadata.date,
        "description": metadata.description,
        "confidence": metadata.confidence,
    }
    for name, value in metadata.field_confidence.model_dump().items():
        values[f"field_confidence.{name}"] = value
    return values


@dataclass
class ExtractionResult:
    """Metadata plus LLM usage from an extraction run."""

    metadata: ReceiptMetadata
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0


class Receipt(BaseModel):
    """Full receipt record as stored in the database."""

    id: UUID
    source_id: str
    source_type: str
    source_name: str
    vendor: str
    amount: Decimal
    paid_amount: Decimal | None = None
    currency: str
    receipt_date: date
    description: str | None
    confidence: float
    field_confidence: dict[str, float] = Field(default_factory=dict)
    # Fields a person has edited; re-extraction never writes them.
    manual_fields: list[str] = Field(default_factory=list)
    pdf_path: str
    email_subject: str | None
    email_sender: str | None
    email_date: datetime | None
    file_name: str | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_amount(self) -> Decimal:
        """The paid amount when known, otherwise the total.

        Search filters on the same expression: COALESCE(paid_amount, amount).
        """
        return self.amount if self.paid_amount is None else self.paid_amount


class IngestLogEntry(BaseModel):
    """A record of an ingest attempt (success, failure, or skip)."""

    id: UUID
    source_id: str
    source_type: str
    status: Literal["started", "success", "failed", "skipped", "queued"]
    receipt_id: UUID | None
    vendor: str | None
    amount: Decimal | None
    paid_amount: Decimal | None = None
    email_subject: str | None
    email_sender: str | None
    email_date: datetime | None
    error_message: str | None
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None
    llm_cache_read_tokens: int | None = None
    llm_requests: int | None = None
    llm_model: str | None = None
    source_name: str | None = None
    attempt: int = 1
    counts_attempt: bool = True
    next_retry_at: datetime | None = None
    error_class: Literal["transient", "content", "config"] | None = None
    stored_ref: str | None = None
    retry_request_id: UUID | None = None
    # A link-only skip's hosted receipt URL (may carry a personal token).
    receipt_link: str | None = None
    created_at: datetime
