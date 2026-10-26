"""Domain and extraction models for receipt indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
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
    status: Literal["success", "failed", "skipped"]
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
    created_at: datetime
