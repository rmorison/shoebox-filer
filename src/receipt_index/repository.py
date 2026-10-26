"""Database query functions for receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from receipt_index.models import IngestLogEntry, Receipt, SyncState

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


def is_source_id_known(
    conn: psycopg.Connection[dict[str, Any]], source_id: str
) -> bool:
    """Return whether a source ID has a receipt or any ingest_log row.

    The same rule as ``get_processed_source_ids``, for one ID: the re-check
    made after taking an item lock.
    """
    cur = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM receipt.receipts WHERE source_id = %(id)s) "
        "OR EXISTS (SELECT 1 FROM receipt.ingest_log WHERE source_id = %(id)s) "
        "AS known",
        {"id": source_id},
    )
    row = cur.fetchone()
    return bool(row and row["known"])


def get_stale_started(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    older_than_seconds: int = 3600,
) -> list[dict[str, Any]]:
    """Return items whose latest ingest_log row is an old ``started`` row.

    Such an item was being processed when its process died. It counts as
    known, so nothing picks it up again until the retry machinery exists;
    the one-shot ingest lists these in a warning.
    """
    cur = conn.execute(
        """\
        SELECT l.source_id, l.source_name, l.created_at
        FROM receipt.ingest_log l
        WHERE l.status = 'started'
          AND l.created_at < now() - make_interval(secs => %(secs)s)
          AND NOT EXISTS (
              SELECT 1 FROM receipt.ingest_log later
              WHERE later.source_id = l.source_id
                AND (later.created_at, later.id) > (l.created_at, l.id))
          AND NOT EXISTS (
              SELECT 1 FROM receipt.receipts r WHERE r.source_id = l.source_id)
        ORDER BY l.created_at, l.id
        """,
        {"secs": older_than_seconds},
    )
    return list(cur.fetchall())


def get_sync_state(
    conn: psycopg.Connection[dict[str, Any]],
    source_name: str,
    folder: str,
) -> SyncState | None:
    """Return a source's stored read position, or None if it has none."""
    cur = conn.execute(
        "SELECT * FROM receipt.sync_state "
        "WHERE source_name = %(source_name)s AND folder = %(folder)s",
        {"source_name": source_name, "folder": folder},
    )
    row = cur.fetchone()
    conn.commit()
    return SyncState.model_validate(row) if row else None


def save_imap_cursor(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    source_name: str,
    folder: str,
    uidvalidity: int,
    last_uid: int,
    reset: bool = False,
) -> None:
    """Store an IMAP source's cursor.

    ``uidvalidity`` and ``last_uid`` are always written together: a row must
    never hold a new UIDVALIDITY with a UID from the old numbering.
    ``reset`` counts a UIDVALIDITY change.
    """
    conn.execute(
        """\
        INSERT INTO receipt.sync_state (
            source_name, source_type, folder, imap_uidvalidity, imap_last_uid,
            resets
        ) VALUES (
            %(source_name)s, 'imap', %(folder)s, %(uidvalidity)s, %(last_uid)s,
            %(resets)s
        )
        ON CONFLICT ON CONSTRAINT uq_sync_state_source_folder DO UPDATE SET
            imap_uidvalidity = EXCLUDED.imap_uidvalidity,
            imap_last_uid = EXCLUDED.imap_last_uid,
            resets = receipt.sync_state.resets + EXCLUDED.resets
        """,
        {
            "source_name": source_name,
            "folder": folder,
            "uidvalidity": uidvalidity,
            "last_uid": last_uid,
            "resets": 1 if reset else 0,
        },
    )
    conn.commit()


def save_drive_cursor(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    source_name: str,
    folder: str,
    page_token: str,
    account_id: str,
    reset: bool = False,
) -> None:
    """Store a Drive source's changes-feed token and the account it belongs to.

    ``reset`` counts a re-bootstrap (an invalid token or a changed account).
    """
    conn.execute(
        """\
        INSERT INTO receipt.sync_state (
            source_name, source_type, folder, drive_page_token, drive_account_id,
            resets
        ) VALUES (
            %(source_name)s, 'gdrive', %(folder)s, %(page_token)s, %(account_id)s,
            %(resets)s
        )
        ON CONFLICT ON CONSTRAINT uq_sync_state_source_folder DO UPDATE SET
            drive_page_token = EXCLUDED.drive_page_token,
            drive_account_id = EXCLUDED.drive_account_id,
            resets = receipt.sync_state.resets + EXCLUDED.resets
        """,
        {
            "source_name": source_name,
            "folder": folder,
            "page_token": page_token,
            "account_id": account_id,
            "resets": 1 if reset else 0,
        },
    )
    conn.commit()


def record_poll(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    source_name: str,
    source_type: str,
    folder: str,
    error: str | None,
) -> None:
    """Record that a source was read: ``last_poll_at``, then success or error."""
    conn.execute(
        """\
        INSERT INTO receipt.sync_state (
            source_name, source_type, folder, last_poll_at,
            last_success_at, last_error, last_error_at
        ) VALUES (
            %(source_name)s, %(source_type)s, %(folder)s, now(),
            CASE WHEN %(error)s::text IS NULL THEN now() END,
            %(error)s::text,
            CASE WHEN %(error)s::text IS NOT NULL THEN now() END
        )
        ON CONFLICT ON CONSTRAINT uq_sync_state_source_folder DO UPDATE SET
            last_poll_at = EXCLUDED.last_poll_at,
            last_success_at = COALESCE(
                EXCLUDED.last_success_at, receipt.sync_state.last_success_at),
            last_error = COALESCE(EXCLUDED.last_error, receipt.sync_state.last_error),
            last_error_at = COALESCE(
                EXCLUDED.last_error_at, receipt.sync_state.last_error_at)
        """,
        {
            "source_name": source_name,
            "source_type": source_type,
            "folder": folder,
            "error": error,
        },
    )
    conn.commit()


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
    source_name: str | None = None,
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
            llm_requests, llm_model, paid_amount, source_name
        ) VALUES (
            %(source_id)s, %(source_type)s, %(status)s, %(receipt_id)s,
            %(vendor)s, %(amount)s, %(email_subject)s, %(email_sender)s,
            %(email_date)s, %(error_message)s,
            %(llm_input_tokens)s, %(llm_output_tokens)s, %(llm_cache_read_tokens)s,
            %(llm_requests)s, %(llm_model)s, %(paid_amount)s, %(source_name)s
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
            "source_name": source_name,
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
