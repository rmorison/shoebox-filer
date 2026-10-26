"""Database query functions for receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from receipt_index.models import IngestLogEntry, Receipt

if TYPE_CHECKING:
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID

    import psycopg

# The effective amount: paid when known, the total otherwise. The expression
# matches idx_receipts_effective_amount, so keep the two in step.
_EFFECTIVE_AMOUNT = "COALESCE(paid_amount, amount)"


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
    paid_amount: Decimal | None = None,
    field_confidence: dict[str, float] | None = None,
) -> Receipt:
    """Insert a receipt row and return the validated Receipt model."""
    cur = conn.execute(
        """\
        INSERT INTO receipt.receipts (
            source_id, source_type, source_name, vendor, amount, currency,
            receipt_date, description, confidence, pdf_path,
            email_subject, email_sender, email_date, file_name,
            paid_amount, field_confidence
        ) VALUES (
            %(source_id)s, %(source_type)s, %(source_name)s,
            %(vendor)s, %(amount)s, %(currency)s,
            %(receipt_date)s, %(description)s, %(confidence)s, %(pdf_path)s,
            %(email_subject)s, %(email_sender)s, %(email_date)s, %(file_name)s,
            %(paid_amount)s, %(field_confidence)s
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
            "paid_amount": paid_amount,
            "field_confidence": Jsonb(field_confidence or {}),
        },
    )
    row = cur.fetchone()
    conn.commit()
    return Receipt.model_validate(row)


def _receipt_filters(
    *,
    vendor: str | None,
    amount: Decimal | None,
    amount_min: Decimal | None,
    amount_max: Decimal | None,
    date_from: date | None,
    date_to: date | None,
    source_name: str | None,
) -> tuple[str, dict[str, object]]:
    """Build the WHERE clause and parameters shared by search and count."""
    clauses: list[str] = []
    params: dict[str, object] = {}

    if vendor is not None:
        clauses.append("vendor ILIKE %(vendor)s")
        params["vendor"] = f"%{vendor}%"

    if amount is not None:
        clauses.append(f"{_EFFECTIVE_AMOUNT} = %(amount)s")
        params["amount"] = amount

    if amount_min is not None:
        clauses.append(f"{_EFFECTIVE_AMOUNT} >= %(amount_min)s")
        params["amount_min"] = amount_min

    if amount_max is not None:
        clauses.append(f"{_EFFECTIVE_AMOUNT} <= %(amount_max)s")
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
    return where, params


def _page_clause(
    params: dict[str, object], limit: int | None, offset: int | None
) -> str:
    """Return LIMIT/OFFSET SQL for the given values, adding them to params."""
    sql = ""
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    if offset is not None:
        sql += " OFFSET %(offset)s"
        params["offset"] = offset
    return sql


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
    limit: int | None = None,
    offset: int | None = None,
) -> list[Receipt]:
    """Search receipts with optional filters, combined with AND.

    Results are ordered newest first, then by vendor, then by id, so that
    ``limit``/``offset`` pages are stable between calls.
    """
    where, params = _receipt_filters(
        vendor=vendor,
        amount=amount,
        amount_min=amount_min,
        amount_max=amount_max,
        date_from=date_from,
        date_to=date_to,
        source_name=source_name,
    )
    sql = (
        f"SELECT * FROM receipt.receipts WHERE {where} "
        "ORDER BY receipt_date DESC, vendor ASC, id ASC"
    ) + _page_clause(params, limit, offset)

    cur = conn.execute(sql, params)
    return [Receipt.model_validate(row) for row in cur.fetchall()]


def count_receipts(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    vendor: str | None = None,
    amount: Decimal | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    source_name: str | None = None,
) -> int:
    """Count the receipts that ``search_receipts`` would return unpaged."""
    where, params = _receipt_filters(
        vendor=vendor,
        amount=amount,
        amount_min=amount_min,
        amount_max=amount_max,
        date_from=date_from,
        date_to=date_to,
        source_name=source_name,
    )
    cur = conn.execute(
        f"SELECT COUNT(*) AS n FROM receipt.receipts WHERE {where}", params
    )
    row = cur.fetchone()
    return int(row["n"]) if row else 0


def insert_ingest_log(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    source_id: str,
    source_type: str,
    status: str,
    receipt_id: UUID | None = None,
    vendor: str | None = None,
    amount: Decimal | None = None,
    paid_amount: Decimal | None = None,
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
            llm_requests, llm_model, paid_amount
        ) VALUES (
            %(source_id)s, %(source_type)s, %(status)s, %(receipt_id)s,
            %(vendor)s, %(amount)s, %(email_subject)s, %(email_sender)s,
            %(email_date)s, %(error_message)s,
            %(llm_input_tokens)s, %(llm_output_tokens)s, %(llm_cache_read_tokens)s,
            %(llm_requests)s, %(llm_model)s, %(paid_amount)s
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
            "paid_amount": paid_amount,
        },
    )
    row = cur.fetchone()
    conn.commit()
    return IngestLogEntry.model_validate(row)


def get_ingest_failures(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> list[IngestLogEntry]:
    """Return failed ingest log entries, newest first (then by id)."""
    params: dict[str, object] = {}
    sql = (
        "SELECT * FROM receipt.ingest_log WHERE status = 'failed' "
        "ORDER BY created_at DESC, id ASC"
    ) + _page_clause(params, limit, offset)
    cur = conn.execute(sql, params)
    return [IngestLogEntry.model_validate(row) for row in cur.fetchall()]


def count_ingest_failures(conn: psycopg.Connection[dict[str, Any]]) -> int:
    """Count failed ingest log entries."""
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM receipt.ingest_log WHERE status = 'failed'"
    )
    row = cur.fetchone()
    return int(row["n"]) if row else 0


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
