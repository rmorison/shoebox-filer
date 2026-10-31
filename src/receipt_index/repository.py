"""Database query functions for receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from receipt_index.adapters.gdrive import UNSUPPORTED_REASON
from receipt_index.models import (
    FAILED_RUN_KEY,
    MIN_RECEIPT_CONFIDENCE,
    IngestLogEntry,
    Provenance,
    Receipt,
    ReceiptEvent,
    SyncState,
)
from receipt_index.store import LOCAL_SCHEME, split_ref

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from decimal import Decimal

    import psycopg

    from receipt_index.models import ActorKind, EventType
    from receipt_index.pdf_validate import PdfStatus

DocumentCertainty = Literal["certain", "ambiguous"]
ProvenanceFileRole = Literal["message", "attachment", "drive_original", "drive_export"]

# The manual_fields name of a field_confidence entry, where they differ.
_MANUAL_NAME = {"date": "receipt_date"}

# The effective amount: paid when known, the total otherwise. The expression
# matches idx_receipts_effective_amount, so keep the two in step.
_EFFECTIVE_AMOUNT = "COALESCE(paid_amount, amount)"

# A receipt with its refunded and net paid figures (null unless a purchase).
# receipts keeps its own name, unaliased: the filters and _NEEDS_REVIEW refer
# to its columns bare or as receipts.<column>, and no view column shares a
# name with one of them.
_WITH_NET = (
    "SELECT receipts.*, n.refunded, n.net_paid FROM receipt.receipts"
    " LEFT JOIN receipt.v_receipt_net n ON n.receipt_id = receipts.id"
)

# An ingest_log row (aliased l) whose item has no receipt: no provenance row.
_NO_PROVENANCE = (
    "NOT EXISTS (SELECT 1 FROM receipt.provenance p WHERE p.source_id = l.source_id)"
)


def get_processed_source_ids(
    conn: psycopg.Connection[dict[str, Any]],
) -> set[str]:
    """Return source_ids already in provenance or ingest_log.

    This provides full idempotency — we won't re-process items that were
    previously ingested, skipped (not a receipt), or failed. A source item
    that evidences a receipt has a provenance row (evidence-store design, §6
    step 3), whether or not ``receipts`` still names it.
    """
    cur = conn.execute(
        "SELECT source_id FROM receipt.provenance "
        "UNION "
        "SELECT source_id FROM receipt.ingest_log"
    )
    return {str(row["source_id"]) for row in cur.fetchall()}


def is_source_id_known(
    conn: psycopg.Connection[dict[str, Any]], source_id: str
) -> bool:
    """Return whether a source ID has a provenance row or any ingest_log row.

    The same rule as ``get_processed_source_ids``, for one ID: the re-check
    made after taking an item lock.
    """
    cur = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM receipt.provenance WHERE source_id = %(id)s) "
        "OR EXISTS (SELECT 1 FROM receipt.ingest_log WHERE source_id = %(id)s) "
        "AS known",
        {"id": source_id},
    )
    row = cur.fetchone()
    return bool(row and row["known"])


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
    order_number: str | None = None,
    payment_method: str | None = None,
    payment_last4: str | None = None,
    payer: str | None = None,
    kind: str = "purchase",
    commit: bool = True,
) -> Receipt:
    """Insert a receipt row and return the validated Receipt model.

    With ``commit=False`` the caller commits, so that the receipt and its
    extraction row land together.
    """
    cur = conn.execute(
        """\
        INSERT INTO receipt.receipts (
            source_id, source_type, source_name, vendor, amount, currency,
            receipt_date, description, confidence, pdf_path,
            email_subject, email_sender, email_date, file_name,
            paid_amount, field_confidence,
            order_number, payment_method, payment_last4, payer, kind
        ) VALUES (
            %(source_id)s, %(source_type)s, %(source_name)s,
            %(vendor)s, %(amount)s, %(currency)s,
            %(receipt_date)s, %(description)s, %(confidence)s, %(pdf_path)s,
            %(email_subject)s, %(email_sender)s, %(email_date)s, %(file_name)s,
            %(paid_amount)s, %(field_confidence)s,
            %(order_number)s, %(payment_method)s, %(payment_last4)s, %(payer)s,
            %(kind)s
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
            "order_number": order_number,
            "payment_method": payment_method,
            "payment_last4": payment_last4,
            "payer": payer,
            "kind": kind,
        },
    )
    row = cur.fetchone()
    if commit:
        conn.commit()
    return Receipt.model_validate(row)


def insert_extraction(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    receipt_id: UUID,
    origin: str,
    input_kind: str,
    prompt_version: int,
    llm_model: str,
    output: dict[str, Any],
    applied_fields: list[str],
    replaced: dict[str, Any],
    llm_input_tokens: int | None,
    llm_output_tokens: int | None,
    llm_cache_read_tokens: int | None,
    llm_requests: int | None,
    input_file_id: UUID | None = None,
) -> None:
    """Append one extraction run to the history. The caller commits.

    The table is append-only: the guard trigger refuses any later change.
    """
    conn.execute(
        """\
        INSERT INTO receipt.extractions (
            receipt_id, origin, input_kind, input_file_id, prompt_version,
            llm_model, output, applied_fields, replaced,
            llm_input_tokens, llm_output_tokens, llm_cache_read_tokens,
            llm_requests
        ) VALUES (
            %(receipt_id)s, %(origin)s, %(input_kind)s, %(input_file_id)s,
            %(prompt_version)s, %(llm_model)s, %(output)s, %(applied_fields)s,
            %(replaced)s, %(llm_input_tokens)s, %(llm_output_tokens)s,
            %(llm_cache_read_tokens)s, %(llm_requests)s
        )
        """,
        {
            "receipt_id": receipt_id,
            "origin": origin,
            "input_kind": input_kind,
            "input_file_id": input_file_id,
            "prompt_version": prompt_version,
            "llm_model": llm_model,
            "output": Jsonb(output),
            "applied_fields": applied_fields,
            "replaced": Jsonb(replaced),
            "llm_input_tokens": llm_input_tokens,
            "llm_output_tokens": llm_output_tokens,
            "llm_cache_read_tokens": llm_cache_read_tokens,
            "llm_requests": llm_requests,
        },
    )


def _receipt_filters(
    *,
    vendor: str | None,
    amount: Decimal | None,
    amount_min: Decimal | None,
    amount_max: Decimal | None,
    date_from: date | None,
    date_to: date | None,
    source_name: str | None,
    needs_review: bool = False,
    order_number: str | None = None,
    last4: str | None = None,
    payer: str | None = None,
    kinds: Sequence[str] | None = None,
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

    if order_number is not None:
        clauses.append("order_number = %(order_number)s")
        params["order_number"] = order_number

    if last4 is not None:
        clauses.append("payment_last4 = %(last4)s")
        params["last4"] = last4

    if payer is not None:
        clauses.append("payer ILIKE %(payer)s")
        params["payer"] = f"%{_escape_like(payer)}%"

    if kinds:
        clauses.append("kind = ANY(%(kinds)s)")
        params["kinds"] = list(kinds)

    if needs_review:
        clauses.append(_NEEDS_REVIEW)

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
    needs_review: bool = False,
    order_number: str | None = None,
    last4: str | None = None,
    payer: str | None = None,
    kinds: Sequence[str] | None = None,
) -> list[Receipt]:
    """Search receipts with optional filters, combined with AND.

    Results are ordered newest first, then by vendor, then by id, so that
    ``limit``/``offset`` pages are stable between calls. ``order_number``
    and ``last4`` match exactly; ``payer`` is a case-insensitive substring.
    ``kinds`` keeps only those kinds (every kind when None or empty).
    A purchase comes with its ``refunded`` and ``net_paid``. ``needs_review``
    keeps receipts still missing a paid amount or whose latest extraction
    failed or was gated, lowest paid-amount confidence first.
    """
    where, params = _receipt_filters(
        vendor=vendor,
        amount=amount,
        amount_min=amount_min,
        amount_max=amount_max,
        date_from=date_from,
        date_to=date_to,
        source_name=source_name,
        needs_review=needs_review,
        order_number=order_number,
        last4=last4,
        payer=payer,
        kinds=kinds,
    )
    order = (
        _NEEDS_REVIEW_ORDER if needs_review else "receipt_date DESC, vendor ASC, id ASC"
    )
    sql = (f"{_WITH_NET} WHERE {where} ORDER BY {order}") + _page_clause(
        params, limit, offset
    )

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
    needs_review: bool = False,
    order_number: str | None = None,
    last4: str | None = None,
    payer: str | None = None,
    kinds: Sequence[str] | None = None,
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
        needs_review=needs_review,
        order_number=order_number,
        last4=last4,
        payer=payer,
        kinds=kinds,
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
    attempt: int = 1,
    counts_attempt: bool = True,
    next_retry_at: datetime | None = None,
    error_class: str | None = None,
    stored_ref: str | None = None,
    retry_request_id: UUID | None = None,
    receipt_link: str | None = None,
    commit: bool = True,
) -> IngestLogEntry:
    """Insert an ingest log entry and return the validated model.

    With ``commit=False`` the caller commits.
    """
    cur = conn.execute(
        """\
        INSERT INTO receipt.ingest_log (
            source_id, source_type, status, receipt_id, vendor, amount,
            email_subject, email_sender, email_date, error_message,
            llm_input_tokens, llm_output_tokens, llm_cache_read_tokens,
            llm_requests, llm_model, paid_amount, source_name,
            attempt, counts_attempt, next_retry_at, error_class, stored_ref,
            retry_request_id, receipt_link
        ) VALUES (
            %(source_id)s, %(source_type)s, %(status)s, %(receipt_id)s,
            %(vendor)s, %(amount)s, %(email_subject)s, %(email_sender)s,
            %(email_date)s, %(error_message)s,
            %(llm_input_tokens)s, %(llm_output_tokens)s, %(llm_cache_read_tokens)s,
            %(llm_requests)s, %(llm_model)s, %(paid_amount)s, %(source_name)s,
            %(attempt)s, %(counts_attempt)s, %(next_retry_at)s, %(error_class)s,
            %(stored_ref)s, %(retry_request_id)s, %(receipt_link)s
        )
        RETURNING *
        """,
        {
            "attempt": attempt,
            "counts_attempt": counts_attempt,
            "next_retry_at": next_retry_at,
            "error_class": error_class,
            "stored_ref": stored_ref,
            "retry_request_id": retry_request_id,
            "receipt_link": receipt_link,
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
    if commit:
        conn.commit()
    return IngestLogEntry.model_validate(row)


# Each item's latest ingest_log row: the item's state (design section 6).
_LATEST = """\
SELECT DISTINCT ON (source_id) *
FROM receipt.ingest_log
ORDER BY source_id, created_at DESC, id DESC
"""

FailureStatus = Literal[
    "failed", "retrying", "terminal", "skipped", "queued", "link-only"
]

# A skip whose receipt is only a link to a hosted receipt (never followed).
_LINK_ONLY = "l.status = 'skipped' AND l.receipt_link IS NOT NULL"

# Each --status value, over the latest row.
_FAILURE_STATUS = {
    "failed": "l.status = 'failed'",
    "retrying": "l.status = 'failed' AND l.next_retry_at IS NOT NULL",
    "terminal": "l.status = 'failed' AND l.next_retry_at IS NULL",
    "skipped": "l.status = 'skipped'",
    "queued": "l.status = 'queued'",
    "link-only": _LINK_ONLY,
}


def _failure_filters(
    status: FailureStatus,
    since: datetime | None,
    source_name: str | None,
) -> tuple[str, dict[str, object]]:
    clauses = [
        _FAILURE_STATUS[status],
        _NO_PROVENANCE,
    ]
    params: dict[str, object] = {}
    if since is not None:
        clauses.append("l.created_at >= %(since)s")
        params["since"] = since
    if source_name is not None:
        clauses.append("l.source_name = %(source_name)s")
        params["source_name"] = source_name
    return " AND ".join(clauses), params


def get_ingest_failures(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    status: FailureStatus = "failed",
    since: datetime | None = None,
    source_name: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[IngestLogEntry]:
    """Items whose latest ingest_log row matches ``status``, newest first.

    One entry per item: its latest row. ``failed`` covers ``retrying`` (due
    again later) and ``terminal`` (not retried automatically).
    """
    where, params = _failure_filters(status, since, source_name)
    sql = (
        f"SELECT * FROM ({_LATEST}) l WHERE {where} ORDER BY created_at DESC, id ASC"
    ) + _page_clause(params, limit, offset)
    cur = conn.execute(sql, params)
    return [IngestLogEntry.model_validate(row) for row in cur.fetchall()]


def count_ingest_failures(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    status: FailureStatus = "failed",
    since: datetime | None = None,
    source_name: str | None = None,
) -> int:
    """Count what ``get_ingest_failures`` would return unpaged."""
    where, params = _failure_filters(status, since, source_name)
    cur = conn.execute(f"SELECT COUNT(*) AS n FROM ({_LATEST}) l WHERE {where}", params)
    row = cur.fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Retries (always-on ingest design, section 6)
# ---------------------------------------------------------------------------


def get_item_history(
    conn: psycopg.Connection[dict[str, Any]],
    source_id: str,
    *,
    limit: int = 100,
) -> list[IngestLogEntry]:
    """An item's ingest_log rows, newest first."""
    cur = conn.execute(
        "SELECT * FROM receipt.ingest_log WHERE source_id = %(id)s "
        "ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
        {"id": source_id, "limit": limit},
    )
    return [IngestLogEntry.model_validate(row) for row in cur.fetchall()]


def has_receipt(conn: psycopg.Connection[dict[str, Any]], source_id: str) -> bool:
    """Whether a receipt exists for this source ID (it has a provenance row)."""
    cur = conn.execute(
        "SELECT 1 FROM receipt.provenance WHERE source_id = %(id)s",
        {"id": source_id},
    )
    return cur.fetchone() is not None


# Due for the retry scheduler, over the latest row: requeued, a failure whose
# time has come, or a started row (whose lock the caller then tries).
_DUE = (
    "(l.status = 'queued'"
    " OR (l.status = 'failed' AND l.next_retry_at <= now())"
    " OR l.status = 'started')"
)


def is_due(entry: IngestLogEntry, now: datetime) -> bool:
    """The ``_DUE`` rule for one latest row, re-checked under the item lock."""
    if entry.status in ("queued", "started"):
        return True
    return (
        entry.status == "failed"
        and entry.next_retry_at is not None
        and entry.next_retry_at <= now
    )


def select_due(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    source_name: str,
    source_type: str,
    configured: list[str] | None = None,
    limit: int | None = None,
) -> list[IngestLogEntry]:
    """Items due for a retry from one source, oldest first.

    A row that names no source (written before rows named their source), or
    a source not in ``configured`` (renamed or removed), belongs to every
    source of its type; the re-fetch tries each of them. The item lock keeps
    two passes from running it twice.
    """
    params: dict[str, object] = {
        "name": source_name,
        "type": source_type,
        # Without the configured names, only rows that name no source fall
        # through.
        "configured": list(configured) if configured is not None else None,
    }
    sql = (
        f"SELECT * FROM ({_LATEST}) l WHERE {_DUE} "
        "AND (l.source_name = %(name)s"
        " OR ((l.source_name IS NULL"
        " OR (%(configured)s::text[] IS NOT NULL"
        " AND l.source_name <> ALL(%(configured)s::text[])))"
        " AND l.source_type = %(type)s)) "
        f"AND {_NO_PROVENANCE} "
        "ORDER BY created_at ASC, id ASC"
    )
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    cur = conn.execute(sql, params)
    rows = [IngestLogEntry.model_validate(row) for row in cur.fetchall()]
    conn.commit()
    return rows


@dataclass(frozen=True)
class RetryFilter:
    """What ``retry`` selects. Every set filter must match (AND)."""

    status: Literal["failed", "skipped", "link-only"] = "failed"
    source_ids: tuple[str, ...] = ()
    error_like: str | None = None
    error_class: str | None = None
    since: datetime | None = None
    source_name: str | None = None

    def narrows(self) -> bool:
        """Whether anything narrower than the status is set.

        ``--source`` alone doesn't count: with one configured source it
        matches everything.
        """
        return bool(
            self.source_ids
            or self.error_like
            or self.error_class is not None
            or self.since is not None
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_ids": list(self.source_ids),
            "error_like": self.error_like,
            "error_class": self.error_class,
            "since": self.since.isoformat() if self.since else None,
            "source_name": self.source_name,
        }


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def select_retry_candidates(
    conn: psycopg.Connection[dict[str, Any]], spec: RetryFilter
) -> list[IngestLogEntry]:
    """The items ``retry`` would requeue: their latest rows, oldest first."""
    clauses = [
        _FAILURE_STATUS[spec.status],
        _NO_PROVENANCE,
    ]
    params: dict[str, object] = {}
    if spec.status == "skipped":
        # A file skipped for its type can't be ingested however often it is
        # asked: only skips the model judged are worth a second look.
        clauses.append("COALESCE(l.error_message, '') NOT LIKE %(type_skip)s")
        params["type_skip"] = f"Skipped: {_escape_like(UNSUPPORTED_REASON)} (%"
    if spec.source_ids:
        clauses.append("l.source_id = ANY(%(ids)s)")
        params["ids"] = list(spec.source_ids)
    if spec.error_like is not None:
        clauses.append("l.error_message ILIKE %(like)s")
        params["like"] = f"%{_escape_like(spec.error_like)}%"
    if spec.error_class is not None:
        clauses.append("l.error_class = %(class)s")
        params["class"] = spec.error_class
    if spec.since is not None:
        clauses.append("l.created_at >= %(since)s")
        params["since"] = spec.since
    if spec.source_name is not None:
        clauses.append("l.source_name = %(source_name)s")
        params["source_name"] = spec.source_name
    where = " AND ".join(clauses)
    cur = conn.execute(
        f"SELECT * FROM ({_LATEST}) l WHERE {where} ORDER BY created_at ASC, id ASC",
        params,
    )
    rows = [IngestLogEntry.model_validate(row) for row in cur.fetchall()]
    conn.commit()
    return rows


def insert_retry_request(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    requested_by: str,
    filters: dict[str, Any],
    matched: int,
) -> UUID:
    """Record a requeue, with ``requeued`` 0 until the loop sets it. Commits."""
    cur = conn.execute(
        "INSERT INTO receipt.retry_requests (requested_by, filters, matched, requeued) "
        "VALUES (%(by)s, %(filters)s, %(matched)s, 0) RETURNING id",
        {"by": requested_by, "filters": Jsonb(filters), "matched": matched},
    )
    row = cur.fetchone()
    conn.commit()
    assert row is not None
    return UUID(str(row["id"]))


def set_retry_request_count(
    conn: psycopg.Connection[dict[str, Any]], request_id: UUID, requeued: int
) -> None:
    """Write how many items a requeue reached. Commits."""
    conn.execute(
        "UPDATE receipt.retry_requests SET requeued = %(n)s WHERE id = %(id)s",
        {"n": requeued, "id": request_id},
    )
    conn.commit()


def orphaned_refs(
    conn: psycopg.Connection[dict[str, Any]], source_ids: list[str] | None
) -> dict[str, list[str]]:
    """Stored renditions that failed rows recorded, and that no receipt
    references, by source ID: for these items, or for every item when
    ``source_ids`` is None. The caller checks they still exist."""
    if source_ids is not None and not source_ids:
        return {}
    cur = conn.execute(
        """\
        SELECT DISTINCT l.source_id, l.stored_ref
        FROM receipt.ingest_log l
        WHERE (%(ids)s::text[] IS NULL OR l.source_id = ANY(%(ids)s::text[]))
          AND l.stored_ref IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM receipt.receipts r
              WHERE %(scheme)s || r.pdf_path = l.stored_ref)
        ORDER BY l.source_id, l.stored_ref
        """,
        {"ids": source_ids, "scheme": LOCAL_SCHEME},
    )
    found: dict[str, list[str]] = {}
    for row in cur.fetchall():
        found.setdefault(str(row["source_id"]), []).append(str(row["stored_ref"]))
    conn.commit()
    return found


def get_receipt_by_id(
    conn: psycopg.Connection[dict[str, Any]],
    receipt_id: UUID,
) -> Receipt | None:
    """Look up a single receipt by its UUID; a purchase with its net paid."""
    cur = conn.execute(
        f"{_WITH_NET} WHERE receipts.id = %(id)s",
        {"id": receipt_id},
    )
    row = cur.fetchone()
    if row is None:
        return None
    return Receipt.model_validate(row)


# ---------------------------------------------------------------------------
# Re-extraction (receipt data-model design, §3)
# ---------------------------------------------------------------------------


# The words reextract --refund-candidates looks for: whole words, any case
# (a Postgres regex; \m and \M are its word boundaries). "credit card" is
# not a credit, and "returns policy" is not a return.
REFUND_CANDIDATE_WORDS = (
    "refund",
    "refunds",
    "refunded",
    "credit",
    "credits",
    "credited",
    "return",
    "returned",
    "reversal",
    "reversals",
    "reversed",
    "chargeback",
    "chargebacks",
    "statement",
    "statements",
    "estatement",
    "estatements",
    "cancel",
    "cancelled",
    "canceled",
    "cancellation",
    "cancellations",
)
REFUND_CANDIDATE_PATTERN = r"\m(" + "|".join(REFUND_CANDIDATE_WORDS) + r")\M(?!\s+card)"


def _failed(output: str = "output") -> str:
    """SQL that is true for a failed extraction run's ``output``."""
    return f"{output} ? '{FAILED_RUN_KEY}'"


# Each receipt's latest extraction that didn't fail. A failed run (its output
# holds an "error") never makes a receipt current, so the next run retries it.
_LATEST_GOOD = f"""\
SELECT DISTINCT ON (receipt_id) receipt_id, prompt_version, llm_model
FROM receipt.extractions
WHERE NOT ({_failed()})
ORDER BY receipt_id, created_at DESC, id DESC
"""

# A receipt still needs review when its paid amount isn't known, or when its
# latest run (failed ones included) applied nothing because it failed or was
# gated. A run that applied nothing because nothing was empty doesn't count.
# A paid amount a person set by hand has been reviewed: no run can change it
# (``edit --clear-manual`` puts the receipt back on the list).
_NEEDS_REVIEW = f"""\
NOT ('paid_amount' = ANY(manual_fields)) AND (paid_amount IS NULL OR EXISTS (
    SELECT 1 FROM (
        SELECT e.output FROM receipt.extractions e
        WHERE e.receipt_id = receipts.id
        ORDER BY e.created_at DESC, e.id DESC
        LIMIT 1
    ) latest
    WHERE {_failed("latest.output")}
       OR (latest.output->>'confidence')::numeric < {MIN_RECEIPT_CONFIDENCE}
))"""

# Lowest paid-amount confidence first; receipts with no such entry first of all.
_NEEDS_REVIEW_ORDER = (
    "(field_confidence->>'paid_amount')::numeric ASC NULLS FIRST, "
    "receipt_date DESC, vendor ASC, id ASC"
)


def select_for_reextract(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    prompt_version: int,
    llm_model: str,
    date_from: date | None = None,
    date_to: date | None = None,
    source_name: str | None = None,
    ids: list[UUID] | None = None,
    force: bool = False,
    model_not: str | None = None,
    confidence_below: tuple[str | None, float] | None = None,
    refund_candidates: bool = False,
    limit: int | None = None,
) -> list[Receipt]:
    """The receipts a re-extraction run selects, oldest first.

    Filters combine with AND. Unless ``force``, a receipt is left out when its
    latest extraction that didn't fail has the current prompt version and the
    configured model: a prompt change or a model change makes it stale.
    ``model_not`` keeps receipts whose latest such run used another model (or
    that have none). ``confidence_below`` is ``(field, x)``: the overall
    confidence when ``field`` is None, else that field's entry, missing
    entries counting as below; a receipt where that field is hand-edited
    is left out. ``refund_candidates`` keeps receipts still marked purchase,
    their kind not hand-edited, whose subject, description, vendor or file
    name has a word that a refund, credit or statement often uses.
    """
    clauses: list[str] = []
    params: dict[str, object] = {"version": prompt_version, "model": llm_model}
    if date_from is not None:
        clauses.append("r.receipt_date >= %(date_from)s")
        params["date_from"] = date_from
    if date_to is not None:
        clauses.append("r.receipt_date <= %(date_to)s")
        params["date_to"] = date_to
    if source_name is not None:
        clauses.append("r.source_name = %(source_name)s")
        params["source_name"] = source_name
    if ids:
        clauses.append("r.id = ANY(%(ids)s)")
        params["ids"] = list(ids)
    if not force:
        clauses.append(
            "NOT (l.prompt_version IS NOT DISTINCT FROM %(version)s"
            " AND l.llm_model IS NOT DISTINCT FROM %(model)s)"
        )
    if model_not is not None:
        clauses.append("l.llm_model IS DISTINCT FROM %(model_not)s")
        params["model_not"] = model_not
    if confidence_below is not None:
        field, threshold = confidence_below
        params["below"] = threshold
        if field is None:
            clauses.append("r.confidence < %(below)s")
        else:
            clauses.append(
                "COALESCE((r.field_confidence->>%(below_field)s)::numeric, -1)"
                " < %(below)s"
            )
            params["below_field"] = field
            # A hand-edited field has no entry, but a run can't change it.
            clauses.append("NOT (%(below_manual)s = ANY(r.manual_fields))")
            params["below_manual"] = _MANUAL_NAME.get(field, field)
    if refund_candidates:
        clauses.append(
            "r.kind = 'purchase' AND NOT ('kind' = ANY(r.manual_fields))"
            " AND concat_ws(' ', r.email_subject, r.description, r.vendor,"
            " r.file_name) ~* %(candidate_words)s"
        )
        params["candidate_words"] = REFUND_CANDIDATE_PATTERN
    where = " AND ".join(clauses) if clauses else "TRUE"
    sql = (
        f"WITH l AS ({_LATEST_GOOD}) "
        "SELECT r.* FROM receipt.receipts r LEFT JOIN l ON l.receipt_id = r.id "
        f"WHERE {where} ORDER BY r.receipt_date ASC, r.id ASC"
    )
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    cur = conn.execute(sql, params)
    return [Receipt.model_validate(row) for row in cur.fetchall()]


def is_current(
    conn: psycopg.Connection[dict[str, Any]],
    receipt_id: UUID,
    *,
    prompt_version: int,
    llm_model: str,
) -> bool:
    """Whether a receipt's latest extraction that didn't fail used this
    prompt version and model: the staleness test ``select_for_reextract``
    applies, for one receipt, re-checked under its item lock."""
    cur = conn.execute(
        f"WITH l AS ({_LATEST_GOOD}) SELECT 1 FROM l "
        "WHERE l.receipt_id = %(id)s AND l.prompt_version = %(version)s "
        "AND l.llm_model = %(model)s",
        {"id": receipt_id, "version": prompt_version, "model": llm_model},
    )
    return cur.fetchone() is not None


def lock_receipt(
    conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID
) -> Receipt | None:
    """Read a receipt with ``FOR UPDATE``, in the caller's transaction."""
    cur = conn.execute(
        "SELECT * FROM receipt.receipts WHERE id = %(id)s FOR UPDATE",
        {"id": receipt_id},
    )
    row = cur.fetchone()
    return None if row is None else Receipt.model_validate(row)


def get_extraction_history(
    conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID
) -> tuple[bool, set[str]]:
    """Whether ingest recorded the receipt's extraction, and every field any
    of its extractions applied."""
    cur = conn.execute(
        """\
        SELECT bool_or(origin = 'ingest') AS ingested,
               COALESCE(array_agg(DISTINCT f) FILTER (WHERE f IS NOT NULL),
                        '{}') AS applied
        FROM receipt.extractions
        LEFT JOIN LATERAL unnest(applied_fields) AS f ON TRUE
        WHERE receipt_id = %(id)s
        """,
        {"id": receipt_id},
    )
    row = cur.fetchone()
    if row is None:
        return False, set()
    return bool(row["ingested"]), set(row["applied"] or [])


# The receipt columns re-extraction may write (field_confidence apart).
_REEXTRACT_COLUMNS = frozenset(
    {
        "vendor",
        "amount",
        "paid_amount",
        "currency",
        "receipt_date",
        "description",
        "order_number",
        "payment_method",
        "payment_last4",
        "payer",
        "kind",
        "confidence",
    }
)


def update_receipt_fields(
    conn: psycopg.Connection[dict[str, Any]],
    receipt_id: UUID,
    columns: dict[str, Any],
    field_confidence: dict[str, float] | None = None,
) -> None:
    """Write re-extracted values to one receipt. The caller commits."""
    from psycopg import sql

    unknown = set(columns) - _REEXTRACT_COLUMNS
    if unknown:
        raise ValueError(f"not a re-extractable column: {sorted(unknown)}")
    sets = [
        sql.SQL("{} = {}").format(sql.Identifier(name), sql.Placeholder(name))
        for name in sorted(columns)
    ]
    params: dict[str, Any] = dict(columns)
    if field_confidence is not None:
        sets.append(sql.SQL("field_confidence = {}").format(sql.Placeholder("fc")))
        params["fc"] = Jsonb(field_confidence)
    if not sets:
        return
    params["id"] = receipt_id
    conn.execute(
        sql.SQL("UPDATE receipt.receipts SET {} WHERE id = {}").format(
            sql.SQL(", ").join(sets), sql.Placeholder("id")
        ),
        params,
    )


# The receipt columns a person may edit by hand (receipt data-model design, §4).
_EDITABLE_COLUMNS = frozenset(
    {"paid_amount", "vendor", "receipt_date", "order_number", "kind"}
)


def update_edited_fields(
    conn: psycopg.Connection[dict[str, Any]],
    receipt_id: UUID,
    columns: dict[str, Any],
    *,
    manual_fields: list[str],
    field_confidence: dict[str, float],
) -> Receipt:
    """Write a hand edit (or a cleared mark) to one receipt. The caller commits."""
    from psycopg import sql

    unknown = set(columns) - _EDITABLE_COLUMNS
    if unknown:
        raise ValueError(f"not an editable column: {sorted(unknown)}")
    sets = [
        sql.SQL("{} = {}").format(sql.Identifier(name), sql.Placeholder(name))
        for name in sorted(columns)
    ]
    sets.append(sql.SQL("manual_fields = {}").format(sql.Placeholder("manual")))
    sets.append(sql.SQL("field_confidence = {}").format(sql.Placeholder("fc")))
    params: dict[str, Any] = {
        **columns,
        "manual": manual_fields,
        "fc": Jsonb(field_confidence),
        "id": receipt_id,
    }
    cur = conn.execute(
        sql.SQL("UPDATE receipt.receipts SET {} WHERE id = {} RETURNING *").format(
            sql.SQL(", ").join(sets), sql.Placeholder("id")
        ),
        params,
    )
    return Receipt.model_validate(cur.fetchone())


# How many runs at the current version and model an average needs before it
# stands in for the ingest_log figures.
_MIN_RUNS_FOR_AVERAGE = 10


def token_averages(
    conn: psycopg.Connection[dict[str, Any]], *, llm_model: str, prompt_version: int
) -> dict[str, Any] | None:
    """Measured tokens per extraction, for ``reextract --dry-run`` estimates.

    From extractions at the current prompt version and model once there are
    enough of them, else from the last 30 days of successful ingests with
    this model (the design's measuring query). None when neither has any.
    Returns ``runs``, ``avg_input``, ``avg_output`` and ``basis``.
    """
    cur = conn.execute(
        f"""\
        SELECT count(*) AS runs,
               avg(llm_input_tokens) AS avg_input,
               avg(llm_output_tokens) AS avg_output
        FROM receipt.extractions
        WHERE llm_model = %(model)s AND prompt_version = %(version)s
          AND NOT ({_failed()}) AND llm_input_tokens IS NOT NULL
        """,
        {"model": llm_model, "version": prompt_version},
    )
    row = cur.fetchone()
    if row is not None and int(row["runs"]) >= _MIN_RUNS_FOR_AVERAGE:
        return {**row, "basis": "extractions"}
    cur = conn.execute(
        """\
        SELECT count(*) AS runs,
               avg(llm_input_tokens) AS avg_input,
               avg(llm_output_tokens) AS avg_output
        FROM receipt.ingest_log
        WHERE status = 'success' AND llm_model = %(model)s
          AND llm_input_tokens IS NOT NULL
          AND created_at >= now() - interval '30 days'
        """,
        {"model": llm_model},
    )
    row = cur.fetchone()
    if row is not None and int(row["runs"]) > 0:
        return {**row, "basis": "ingest_log"}
    return None


# ---------------------------------------------------------------------------
# The receipt history (receipt data-model design, §4)
# ---------------------------------------------------------------------------


def insert_event(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    receipt_id: UUID,
    event_type: EventType,
    payload: dict[str, Any],
    actor: str,
    actor_kind: ActorKind,
    reason: str | None = None,
) -> ReceiptEvent:
    """Append one row to ``receipt_events``. The caller commits.

    The event belongs in the same transaction as the change it records.
    """
    cur = conn.execute(
        """\
        INSERT INTO receipt.receipt_events (
            receipt_id, event_type, payload, actor, actor_kind, reason
        ) VALUES (
            %(receipt_id)s, %(event_type)s, %(payload)s, %(actor)s,
            %(actor_kind)s, %(reason)s
        )
        RETURNING *
        """,
        {
            "receipt_id": receipt_id,
            "event_type": event_type,
            "payload": Jsonb(payload),
            "actor": actor,
            "actor_kind": actor_kind,
            "reason": reason,
        },
    )
    return ReceiptEvent.model_validate(cur.fetchone())


def get_receipt_events(
    conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID
) -> list[ReceiptEvent]:
    """A receipt's events, oldest first (UUID v7 breaks a timestamp tie)."""
    cur = conn.execute(
        "SELECT * FROM receipt.receipt_events WHERE receipt_id = %(id)s"
        " ORDER BY created_at, id",
        {"id": receipt_id},
    )
    return [ReceiptEvent.model_validate(row) for row in cur.fetchall()]


def get_cleared_fields(
    conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID
) -> set[str]:
    """Fields whose hand edit was handed back (a ``manual_cleared`` event).

    Re-extraction counts them as machine-extracted, so ``--overwrite`` may
    replace them even when no run ever applied them.
    """
    cur = conn.execute(
        "SELECT DISTINCT payload->>'field' AS field FROM receipt.receipt_events"
        " WHERE receipt_id = %(id)s AND event_type = 'manual_cleared'",
        {"id": receipt_id},
    )
    return {row["field"] for row in cur.fetchall() if row["field"]}


# ---------------------------------------------------------------------------
# Refunds (receipt data-model design, \u00a76)
# ---------------------------------------------------------------------------


def find_refund_purchases(
    conn: psycopg.Connection[dict[str, Any]], refund: Receipt
) -> list[UUID]:
    """The purchases the linking rule would link ``refund`` to.

    Same order number, same vendor (any case), same currency, and a paid
    amount above zero, which leaves out confirmation and shipping emails and
    unpaid invoices that share the order number. The rule links only when
    there is exactly one. The matches are read without a lock, so that
    candidates the rule won't link are never locked: the one it links is
    locked by ``lock_matching_purchase``.
    """
    if refund.order_number is None:
        return []
    cur = conn.execute(
        """\
        SELECT id FROM receipt.receipts
        WHERE kind = 'purchase' AND id <> %(id)s
          AND order_number = %(order_number)s
          AND lower(vendor) = lower(%(vendor)s)
          AND currency = %(currency)s
          AND paid_amount > 0
        ORDER BY id
        """,
        {
            "id": refund.id,
            "order_number": refund.order_number,
            "vendor": refund.vendor,
            "currency": refund.currency,
        },
    )
    return [row["id"] for row in cur.fetchall()]


def find_unlinked_refunds(
    conn: psycopg.Connection[dict[str, Any]], purchase: Receipt
) -> list[Receipt]:
    """Unlinked refunds sharing ``purchase``'s order number, vendor and currency,
    locked ``FOR UPDATE`` in the caller's transaction.

    A refund a person unlinked is left out: only ``refund link`` links it again.
    """
    if purchase.order_number is None:
        return []
    cur = conn.execute(
        """\
        SELECT * FROM receipt.receipts
        WHERE kind = 'refund' AND refund_of IS NULL AND id <> %(id)s
          AND order_number = %(order_number)s
          AND lower(vendor) = lower(%(vendor)s)
          AND currency = %(currency)s
          AND NOT EXISTS (
              SELECT 1 FROM receipt.receipt_events e
              WHERE e.receipt_id = receipts.id
                AND e.event_type = 'refund_unlinked'
          )
        ORDER BY id
        FOR UPDATE
        """,
        {
            "id": purchase.id,
            "order_number": purchase.order_number,
            "vendor": purchase.vendor,
            "currency": purchase.currency,
        },
    )
    return [Receipt.model_validate(row) for row in cur.fetchall()]


def lock_matching_purchase(
    conn: psycopg.Connection[dict[str, Any]], refund: Receipt, purchase_id: UUID
) -> bool:
    """Lock a purchase ``FOR SHARE`` and say whether it still matches ``refund``.

    The whole match is re-checked under the lock (a purchase, the same order
    number, vendor in any case and currency, a paid amount above zero), so a
    concurrent change to any of those either waits for the link or is seen
    by it. Only the one purchase the rule is about to link is locked.
    """
    cur = conn.execute(
        """\
        SELECT 1 FROM receipt.receipts
        WHERE id = %(id)s AND kind = 'purchase'
          AND order_number = %(order_number)s
          AND lower(vendor) = lower(%(vendor)s)
          AND currency = %(currency)s
          AND paid_amount > 0
        FOR SHARE
        """,
        {
            "id": purchase_id,
            "order_number": refund.order_number,
            "vendor": refund.vendor,
            "currency": refund.currency,
        },
    )
    return cur.fetchone() is not None


def was_unlinked(conn: psycopg.Connection[dict[str, Any]], refund_id: UUID) -> bool:
    """Whether a person has unlinked this refund (a ``refund_unlinked`` event)."""
    cur = conn.execute(
        "SELECT 1 FROM receipt.receipt_events WHERE receipt_id = %(id)s"
        " AND event_type = 'refund_unlinked' LIMIT 1",
        {"id": refund_id},
    )
    return cur.fetchone() is not None


def set_refund_of(
    conn: psycopg.Connection[dict[str, Any]],
    refund_id: UUID,
    purchase_id: UUID | None,
    *,
    only_if_unlinked: bool = False,
) -> bool:
    """Link a refund to a purchase, or unlink it (None). The caller commits.

    With ``only_if_unlinked`` (the rule's write), a refund that is already
    linked, by a person meanwhile, is left as it is. Returns whether the row
    was written.
    """
    sql = "UPDATE receipt.receipts SET refund_of = %(purchase)s WHERE id = %(id)s"
    if only_if_unlinked:
        sql += " AND refund_of IS NULL"
    cur = conn.execute(sql, {"purchase": purchase_id, "id": refund_id})
    return cur.rowcount == 1


def has_linked_refunds(
    conn: psycopg.Connection[dict[str, Any]], purchase_id: UUID
) -> bool:
    """Whether any refund is linked to this purchase."""
    cur = conn.execute(
        "SELECT 1 FROM receipt.receipts WHERE refund_of = %(id)s LIMIT 1",
        {"id": purchase_id},
    )
    return cur.fetchone() is not None


def list_refunds(
    conn: psycopg.Connection[dict[str, Any]], *, unlinked: bool = False
) -> list[Receipt]:
    """Every refund, newest first; only those with no purchase if ``unlinked``."""
    where = "kind = 'refund'" + (" AND refund_of IS NULL" if unlinked else "")
    cur = conn.execute(
        f"{_WITH_NET} WHERE {where} ORDER BY receipt_date DESC, vendor ASC, id ASC"
    )
    return [Receipt.model_validate(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Evidence store: stored files, their locations and provenance
# ---------------------------------------------------------------------------


class RenditionConflictError(Exception):
    """Another receipt already has a location at this rendition's locator."""


def record_stored_file(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    sha256: bytes,
    byte_size: int,
    media_type: str,
    pdf_status: PdfStatus | None = None,
    repaired_from_id: UUID | None = None,
) -> UUID:
    """The ``stored_files`` row for these bytes, inserted if it is new.

    Two writers storing the same bytes at once end with one row: the loser's
    insert does nothing, and it reads the winner's row. An existing row
    keeps what it was stored with: the same bytes get the same
    ``pdf_status``, and a row stored before validation keeps NULL.
    """
    row = conn.execute(
        """\
        INSERT INTO receipt.stored_files
            (sha256, byte_size, media_type, pdf_status, repaired_from_id)
        VALUES (%(sha256)s, %(size)s, %(media_type)s, %(pdf_status)s,
                %(repaired_from_id)s)
        ON CONFLICT (sha256) DO NOTHING
        RETURNING id
        """,
        {
            "sha256": sha256,
            "size": byte_size,
            "media_type": media_type,
            "pdf_status": pdf_status,
            "repaired_from_id": repaired_from_id,
        },
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM receipt.stored_files WHERE sha256 = %(sha256)s",
            {"sha256": sha256},
        ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def record_rendition(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    receipt_id: UUID,
    ref: str,
    sha256: bytes,
    byte_size: int,
    media_type: str,
    pdf_status: PdfStatus | None = None,
    repaired_from_id: UUID | None = None,
) -> UUID:
    """Record a receipt's rendition: its stored file, location and file ID.

    Writes the ``stored_files`` row (shared by receipts with the same bytes),
    the receipt's own rendition location, and ``receipts.rendition_file_id``.
    Doesn't commit. A location already recorded for this receipt is kept;
    one held by another receipt raises :class:`RenditionConflictError`.
    ``repaired_from_id`` names the stored PDF a repaired rendition came from.
    """
    file_id = record_stored_file(
        conn,
        sha256=sha256,
        byte_size=byte_size,
        media_type=media_type,
        pdf_status=pdf_status,
        repaired_from_id=repaired_from_id,
    )
    backend, locator = split_ref(ref)
    conn.execute(
        """\
        INSERT INTO receipt.file_locations
            (file_id, backend, locator, purpose, receipt_id, display_path)
        VALUES (%(file_id)s, %(backend)s, %(locator)s, 'rendition',
                %(receipt_id)s, %(locator)s)
        ON CONFLICT DO NOTHING
        """,
        {
            "file_id": file_id,
            "backend": backend,
            "locator": locator,
            "receipt_id": receipt_id,
        },
    )
    held = conn.execute(
        "SELECT receipt_id, file_id FROM receipt.file_locations"
        " WHERE backend = %(backend)s AND locator = %(locator)s",
        {"backend": backend, "locator": locator},
    ).fetchone()
    if held is None or held["receipt_id"] != receipt_id or held["file_id"] != file_id:
        msg = f"{ref} is already recorded for another receipt or other bytes"
        raise RenditionConflictError(msg)
    conn.execute(
        "UPDATE receipt.receipts SET rendition_file_id = %(file_id)s"
        " WHERE id = %(receipt_id)s",
        {"file_id": file_id, "receipt_id": receipt_id},
    )
    return file_id


def location_taken(
    conn: psycopg.Connection[dict[str, Any]], ref: str, receipt_id: UUID
) -> bool:
    """Whether a location other than this receipt's is recorded at ``ref``.

    The same test ``record_rendition`` applies, read only: a location of
    another receipt, or of an original (no receipt), takes the path.
    """
    backend, locator = split_ref(ref)
    row = conn.execute(
        "SELECT 1 FROM receipt.file_locations"
        " WHERE backend = %(backend)s AND locator = %(locator)s"
        " AND receipt_id IS DISTINCT FROM %(receipt_id)s",
        {"backend": backend, "locator": locator, "receipt_id": receipt_id},
    ).fetchone()
    return row is not None


def insert_provenance(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    receipt_id: UUID,
    source_type: str,
    source_name: str,
    source_id: str,
    received_at: datetime,
    email_subject: str | None,
    email_sender: str | None,
    email_date: datetime | None,
    file_name: str | None,
    match_method: str = "first",
    source_sha256: bytes | None = None,
    original_file_id: UUID | None = None,
    receipt_document_file_id: UUID | None = None,
    document_certainty: DocumentCertainty | None = None,
) -> Provenance:
    """Record the source item that created a receipt. Doesn't commit.

    The first item for a receipt is its primary and its home. Its original
    is ``stored`` when ``original_file_id`` is given, else ``pending``.
    """
    row = conn.execute(
        """\
        INSERT INTO receipt.provenance (
            receipt_id, is_primary, source_type, source_name, source_id,
            received_at, email_subject, email_sender, email_date, file_name,
            home_receipt_id, original_status, match_method, source_sha256,
            original_file_id, receipt_document_file_id, document_certainty
        ) VALUES (
            %(receipt_id)s, TRUE, %(source_type)s, %(source_name)s, %(source_id)s,
            %(received_at)s, %(email_subject)s, %(email_sender)s, %(email_date)s,
            %(file_name)s, %(receipt_id)s, %(original_status)s, %(match_method)s,
            %(source_sha256)s, %(original_file_id)s,
            %(receipt_document_file_id)s, %(document_certainty)s
        )
        RETURNING *
        """,
        {
            "original_status": "pending" if original_file_id is None else "stored",
            "source_sha256": source_sha256,
            "original_file_id": original_file_id,
            "receipt_document_file_id": receipt_document_file_id,
            "document_certainty": document_certainty,
            "receipt_id": receipt_id,
            "source_type": source_type,
            "source_name": source_name,
            "source_id": source_id,
            "received_at": received_at,
            "email_subject": email_subject,
            "email_sender": email_sender,
            "email_date": email_date,
            "file_name": file_name,
            "match_method": match_method,
        },
    ).fetchone()
    return Provenance.model_validate(row)


def store_original_ref(
    conn: psycopg.Connection[dict[str, Any]], sha256: bytes
) -> str | None:
    """The reference of a stored original with these bytes, if the store owns one.

    A location the operator's account owns (an adopted dropoff file) doesn't
    count: the operator can delete it, so the same bytes get their own copy.
    """
    row = conn.execute(
        """\
        SELECT l.backend, l.locator
          FROM receipt.stored_files f
          JOIN receipt.file_locations l ON l.file_id = f.id
         WHERE f.sha256 = %(sha256)s
           AND l.purpose = 'original' AND l.owner = 'store'
         ORDER BY l.created_at, l.id
         LIMIT 1
        """,
        {"sha256": sha256},
    ).fetchone()
    if row is None:
        return None
    return f"{row['backend']}:{row['locator']}"


class OriginalLocationConflictError(Exception):
    """An original's location is already recorded for other bytes."""


def record_original(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    ref: str,
    sha256: bytes,
    byte_size: int,
    media_type: str,
    pdf_status: PdfStatus | None = None,
) -> UUID:
    """Record a stored original: its stored file and its location.

    Doesn't commit. A location already recorded for the same file is kept,
    so recording an original again (a retry, or bytes another item stored)
    changes nothing. One recorded for other bytes raises
    :class:`OriginalLocationConflictError`.
    """
    file_id = record_stored_file(
        conn,
        sha256=sha256,
        byte_size=byte_size,
        media_type=media_type,
        pdf_status=pdf_status,
    )
    backend, locator = split_ref(ref)
    conn.execute(
        """\
        INSERT INTO receipt.file_locations
            (file_id, backend, locator, purpose, display_path)
        VALUES (%(file_id)s, %(backend)s, %(locator)s, 'original', %(locator)s)
        ON CONFLICT DO NOTHING
        """,
        {"file_id": file_id, "backend": backend, "locator": locator},
    )
    held = conn.execute(
        "SELECT file_id, purpose FROM receipt.file_locations"
        " WHERE backend = %(backend)s AND locator = %(locator)s",
        {"backend": backend, "locator": locator},
    ).fetchone()
    if held is None or held["file_id"] != file_id or held["purpose"] != "original":
        msg = f"{ref} is already recorded for other bytes"
        raise OriginalLocationConflictError(msg)
    return file_id


def insert_provenance_files(
    conn: psycopg.Connection[dict[str, Any]],
    provenance_id: UUID,
    files: Sequence[tuple[UUID, ProvenanceFileRole, str | None]],
) -> None:
    """Record every file a source item brought, in order. Doesn't commit.

    ``files`` holds (stored file ID, role, raw file name); position 0 is the
    first (the message), 1.. the attachments in MIME order.
    """
    for position, (file_id, role, file_name) in enumerate(files):
        conn.execute(
            """\
            INSERT INTO receipt.provenance_files
                (provenance_id, position, file_id, role, file_name)
            VALUES (%(provenance_id)s, %(position)s, %(file_id)s, %(role)s,
                    %(file_name)s)
            """,
            {
                "provenance_id": provenance_id,
                "position": position,
                "file_id": file_id,
                "role": role,
                "file_name": file_name,
            },
        )


def receipts_without_rendition(
    conn: psycopg.Connection[dict[str, Any]],
) -> list[tuple[UUID, str | None]]:
    """Receipts whose rendition isn't recorded yet: ID and ``pdf_path``."""
    rows = conn.execute(
        "SELECT id, pdf_path FROM receipt.receipts"
        " WHERE rendition_file_id IS NULL ORDER BY created_at, id"
    ).fetchall()
    return [(UUID(str(r["id"])), r["pdf_path"]) for r in rows]
