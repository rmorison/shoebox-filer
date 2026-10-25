"""Database query functions for receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from receipt_index.models import IngestLogEntry, Receipt

if TYPE_CHECKING:
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID

    import psycopg


def get_processed_source_ids(
    conn: psycopg.Connection[dict[str, Any]],
) -> set[str]:
    """Return source_ids already in receipts or ingest_log (skipped/failed).

    This provides full idempotency — we won't re-process messages that were
    previously ingested, skipped (not a receipt), or failed.
    """
    cur = conn.execute(
        "SELECT source_id FROM receipt.receipts "
        "UNION "
        "SELECT source_id FROM receipt.ingest_log"
    )
    return {str(row["source_id"]) for row in cur.fetchall()}


def insert_receipt(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    source_id: str,
    source_type: str,
    source_name: str,
    vendor: str,
    amount: Decimal,
    currency: str,
    receipt_date: date,
    description: str | None,
    confidence: float,
    pdf_path: str,
    email_subject: str | None,
    email_sender: str | None,
    email_date: datetime | None,
    file_name: str | None = None,
) -> Receipt:
    """Insert a receipt row and return the validated Receipt model."""
    cur = conn.execute(
        """\
        INSERT INTO receipt.receipts (
            source_id, source_type, source_name, vendor, amount, currency,
            receipt_date, description, confidence, pdf_path,
            email_subject, email_sender, email_date, file_name
        ) VALUES (
            %(source_id)s, %(source_type)s, %(source_name)s,
            %(vendor)s, %(amount)s, %(currency)s,
            %(receipt_date)s, %(description)s, %(confidence)s, %(pdf_path)s,
            %(email_subject)s, %(email_sender)s, %(email_date)s, %(file_name)s
        )
        RETURNING *
        """,
        {
            "source_id": source_id,
            "source_type": source_type,
            "source_name": source_name,
            "vendor": vendor,
            "amount": amount,
            "currency": currency,
            "receipt_date": receipt_date,
            "description": description,
            "confidence": confidence,
            "pdf_path": pdf_path,
            "email_subject": email_subject,
            "email_sender": email_sender,
            "email_date": email_date,
            "file_name": file_name,
        },
    )
    row = cur.fetchone()
    conn.commit()
    return Receipt.model_validate(row)


def search_receipts(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    vendor: str | None = None,
    amount: Decimal | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    source_name: str | None = None,
) -> list[Receipt]:
    """Search receipts with optional filters, combined with AND."""
    clauses: list[str] = []
    params: dict[str, object] = {}

    if vendor is not None:
        clauses.append("vendor ILIKE %(vendor)s")
        params["vendor"] = f"%{vendor}%"

    if amount is not None:
        clauses.append("amount = %(amount)s")
        params["amount"] = amount

    if amount_min is not None:
        clauses.append("amount >= %(amount_min)s")
        params["amount_min"] = amount_min

    if amount_max is not None:
        clauses.append("amount <= %(amount_max)s")
        params["amount_max"] = amount_max

    if date_from is not None:
        clauses.append("receipt_date >= %(date_from)s")
        params["date_from"] = date_from

    if date_to is not None:
        clauses.append("receipt_date <= %(date_to)s")
        params["date_to"] = date_to

    if source_name is not None:
        clauses.append("source_name = %(source_name)s")
        params["source_name"] = source_name

    where = " AND ".join(clauses) if clauses else "TRUE"
    sql = (
        f"SELECT * FROM receipt.receipts WHERE {where} "
        "ORDER BY receipt_date DESC, vendor ASC"
    )

    cur = conn.execute(sql, params)
    return [Receipt.model_validate(row) for row in cur.fetchall()]


def insert_ingest_log(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    source_id: str,
    source_type: str,
    status: str,
    receipt_id: UUID | None = None,
    vendor: str | None = None,
    amount: Decimal | None = None,
    email_subject: str | None = None,
    email_sender: str | None = None,
    email_date: datetime | None = None,
    error_message: str | None = None,
    llm_input_tokens: int | None = None,
    llm_output_tokens: int | None = None,
    llm_cache_read_tokens: int | None = None,
    llm_requests: int | None = None,
    llm_model: str | None = None,
) -> IngestLogEntry:
    """Insert an ingest log entry and return the validated model."""
    cur = conn.execute(
        """\
        INSERT INTO receipt.ingest_log (
            source_id, source_type, status, receipt_id, vendor, amount,
            email_subject, email_sender, email_date, error_message,
            llm_input_tokens, llm_output_tokens, llm_cache_read_tokens,
            llm_requests, llm_model
        ) VALUES (
            %(source_id)s, %(source_type)s, %(status)s, %(receipt_id)s,
            %(vendor)s, %(amount)s, %(email_subject)s, %(email_sender)s,
            %(email_date)s, %(error_message)s,
            %(llm_input_tokens)s, %(llm_output_tokens)s, %(llm_cache_read_tokens)s,
            %(llm_requests)s, %(llm_model)s
        )
        RETURNING *
        """,
        {
            "source_id": source_id,
            "source_type": source_type,
            "status": status,
            "receipt_id": receipt_id,
            "vendor": vendor,
            "amount": amount,
            "email_subject": email_subject,
            "email_sender": email_sender,
            "email_date": email_date,
            "error_message": error_message,
            "llm_input_tokens": llm_input_tokens,
            "llm_output_tokens": llm_output_tokens,
            "llm_cache_read_tokens": llm_cache_read_tokens,
            "llm_requests": llm_requests,
            "llm_model": llm_model,
        },
    )
    row = cur.fetchone()
    conn.commit()
    return IngestLogEntry.model_validate(row)


def get_ingest_failures(
    conn: psycopg.Connection[dict[str, Any]],
) -> list[IngestLogEntry]:
    """Return all failed ingest log entries, newest first."""
    cur = conn.execute(
        "SELECT * FROM receipt.ingest_log WHERE status = 'failed' "
        "ORDER BY created_at DESC"
    )
    return [IngestLogEntry.model_validate(row) for row in cur.fetchall()]


def get_receipt_by_id(
    conn: psycopg.Connection[dict[str, Any]],
    receipt_id: UUID,
) -> Receipt | None:
    """Look up a single receipt by its UUID."""
    cur = conn.execute(
        "SELECT * FROM receipt.receipts WHERE id = %(id)s",
        {"id": receipt_id},
    )
    row = cur.fetchone()
    if row is None:
        return None
    return Receipt.model_validate(row)
