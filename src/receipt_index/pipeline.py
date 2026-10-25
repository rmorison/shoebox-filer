"""Ingest pipeline orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from receipt_index.extraction import extract_metadata
from receipt_index.renderer import render_pdf
from receipt_index.repository import (
    get_processed_source_ids,
    insert_ingest_log,
    insert_receipt,
)

if TYPE_CHECKING:
    import psycopg
    from pydantic_ai import Agent

    from receipt_index.adapters.base import SourceAdapter
    from receipt_index.models import Receipt, ReceiptMetadata
    from receipt_index.store import FileStore

logger = logging.getLogger(__name__)

_MIN_CONFIDENCE = 0.5


@dataclass
class IngestResult:
    """Summary of an ingest run."""

    processed: int = 0
    skipped: int = 0
    failed: int = 0
    receipts: list[Receipt] = field(default_factory=list)


def run_ingest(
    *,
    conn: psycopg.Connection[dict[str, Any]],
    adapter: SourceAdapter,
    store: FileStore,
    source_name: str,
    source_type: str,
    agent: Agent[None, ReceiptMetadata] | None = None,
    llm_model: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> IngestResult:
    """Orchestrate the receipt ingestion pipeline.

    Fetches unprocessed messages from the adapter, extracts metadata,
    renders PDFs, saves to the file store, and inserts into the database.
    """
    result = IngestResult()
    # Check all source_ids globally — the UNIQUE constraint is on source_id alone,
    # so we must skip any ID that exists regardless of source_name.
    processed_ids = get_processed_source_ids(conn)

    count = 0
    for raw in adapter.fetch_unprocessed(processed_ids):
        if limit is not None and count >= limit:
            break
        count += 1

        if dry_run:
            logger.info("Dry run: would process %s", raw.source_id)
            result.skipped += 1
            continue

        extraction = None
        try:
            extraction = extract_metadata(raw, agent=agent)
            metadata = extraction.metadata

            # Skip non-receipts based on low LLM confidence only.
            # amount == 0 is valid (e.g. prepaid postage receipts).
            if metadata.confidence < _MIN_CONFIDENCE:
                reason = (
                    f"amount={metadata.amount}, confidence={metadata.confidence:.2f}"
                )
                logger.info(
                    "Skipped non-receipt %s (%s): %s",
                    raw.source_id,
                    raw.subject,
                    reason,
                )
                insert_ingest_log(
                    conn,
                    source_id=raw.source_id,
                    source_type=source_type,
                    status="skipped",
                    vendor=metadata.vendor,
                    amount=metadata.amount,
                    email_subject=raw.subject,
                    email_sender=raw.sender,
                    email_date=raw.date if raw.source_type == "imap" else None,
                    error_message=f"Skipped: {reason}",
                    llm_input_tokens=extraction.input_tokens,
                    llm_output_tokens=extraction.output_tokens,
                    llm_cache_read_tokens=extraction.cache_read_tokens,
                    llm_requests=extraction.requests,
                    llm_model=llm_model,
                )
                result.skipped += 1
                continue

            pdf_data = render_pdf(raw)
            pdf_path = store.save(
                metadata.date, metadata.vendor, metadata.amount, pdf_data
            )
            receipt = insert_receipt(
                conn,
                source_id=raw.source_id,
                source_type=source_type,
                source_name=source_name,
                vendor=metadata.vendor,
                amount=metadata.amount,
                currency=metadata.currency,
                receipt_date=metadata.date,
                description=metadata.description,
                confidence=metadata.confidence,
                pdf_path=pdf_path,
                email_subject=raw.subject,
                email_sender=raw.sender,
                email_date=raw.date if raw.source_type == "imap" else None,
                file_name=raw.file_name,
            )
            result.processed += 1
            result.receipts.append(receipt)
            insert_ingest_log(
                conn,
                source_id=raw.source_id,
                source_type=source_type,
                status="success",
                receipt_id=receipt.id,
                vendor=metadata.vendor,
                amount=metadata.amount,
                email_subject=raw.subject,
                email_sender=raw.sender,
                email_date=raw.date if raw.source_type == "imap" else None,
                llm_input_tokens=extraction.input_tokens,
                llm_output_tokens=extraction.output_tokens,
                llm_cache_read_tokens=extraction.cache_read_tokens,
                llm_requests=extraction.requests,
                llm_model=llm_model,
            )
            logger.info(
                "Processed receipt: %s (%s) confidence=%.2f",
                metadata.vendor,
                receipt.id,
                metadata.confidence,
            )
        except Exception as exc:
            # Rollback failed transaction so the ingest_log write succeeds
            conn.rollback()
            try:
                insert_ingest_log(
                    conn,
                    source_id=raw.source_id,
                    source_type=source_type,
                    status="failed",
                    email_subject=raw.subject,
                    email_sender=raw.sender,
                    email_date=raw.date if raw.source_type == "imap" else None,
                    error_message=str(exc),
                    llm_input_tokens=(extraction.input_tokens if extraction else None),
                    llm_output_tokens=(
                        extraction.output_tokens if extraction else None
                    ),
                    llm_cache_read_tokens=(
                        extraction.cache_read_tokens if extraction else None
                    ),
                    llm_requests=(extraction.requests if extraction else None),
                    llm_model=llm_model,
                )
            except Exception:
                logger.warning(
                    "Failed to write ingest log for %s",
                    raw.source_id,
                    exc_info=True,
                )
            logger.warning(
                "Failed to process message %s (subject=%s, sender=%s)",
                raw.source_id,
                raw.subject,
                raw.sender,
                exc_info=True,
            )
            result.failed += 1

    return result
