"""Domain and extraction models for receipt indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, computed_field


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
    status: Literal["started", "success", "failed", "skipped"]
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
    created_at: datetime
