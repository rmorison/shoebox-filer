"""Database query functions for receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from receipt_index.models import (
    FAILED_RUN_KEY,
    MIN_RECEIPT_CONFIDENCE,
    IngestLogEntry,
    Receipt,
    SyncState,
)

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
) -> list[Receipt]:
    """Search receipts with optional filters, combined with AND.

    Results are ordered newest first, then by vendor, then by id, so that
    ``limit``/``offset`` pages are stable between calls. ``needs_review``
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
    )
    order = (
        _NEEDS_REVIEW_ORDER if needs_review else "receipt_date DESC, vendor ASC, id ASC"
    )
    sql = (
        f"SELECT * FROM receipt.receipts WHERE {where} ORDER BY {order}"
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
    needs_review: bool = False,
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


# ---------------------------------------------------------------------------
# Re-extraction (receipt data-model design, §3)
# ---------------------------------------------------------------------------


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
_NEEDS_REVIEW = f"""\
(paid_amount IS NULL OR EXISTS (
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
    limit: int | None = None,
) -> list[Receipt]:
    """The receipts a re-extraction run selects, oldest first.

    Filters combine with AND. Unless ``force``, a receipt is left out when its
    latest extraction that didn't fail has the current prompt version and the
    configured model: a prompt change or a model change makes it stale.
    ``model_not`` keeps receipts whose latest such run used another model (or
    that have none). ``confidence_below`` is ``(field, x)``: the overall
    confidence when ``field`` is None, else that field's entry, missing
    entries counting as below.
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
