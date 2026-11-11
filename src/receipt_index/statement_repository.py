"""Database writes and the change feed for statements (migration 000023).

A statement is one original PDF. Every processing run writes one revision,
with its accounts and the lines of its clean accounts; the statement row
points at its current revision and copies its status and type. Revision
numbers and each statement's place in the feed (``change_seq``) come from one
counter, drawn by triggers, so no caller sets either.

The statement row and its first revision reference each other, so a new
statement is written in one transaction in this order (the plan's U2,
"Insert order"): :func:`new_statement_id`, :func:`insert_revision`, the
accounts and lines, :func:`create_statement`, then :func:`add_source`. A
re-run inserts the revision, accounts and lines, then calls
:func:`set_current_revision`. The foreign keys and the check that a
statement's status and type match its current revision run at commit. The
caller commits, except in :func:`republish_all`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID

    import psycopg

StatementStatus = Literal["complete", "incomplete", "held", "rejected"]
Origin = Literal["ingest", "rerun"]


@dataclass(frozen=True)
class RevisionRow:
    """One processing run of a statement, as written."""

    origin: Origin
    document_type: str
    status: StatementStatus
    hold_reason: str | None = None
    rejected_bound: str | None = None
    masked_runs: int = 0
    period_start: date | None = None
    period_end: date | None = None
    printed_account_count: int | None = None
    pages: int | None = None
    llm_model: str | None = None
    prompt_version: str | None = None
    first_pass_version: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class AccountRow:
    """One account of a revision, as written."""

    ordinal: int
    occurrence: int
    account_type: str
    status: str
    content_digest: bytes
    institution: str | None = None
    last4: str | None = None
    currency: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    held_by_statement: bool = False
    failure_class: str | None = None
    difference: Decimal | None = None
    first_page: int | None = None
    last_page: int | None = None
    line_order: str | None = None
    date_grace_days: int = 0
    beginning_balance: Decimal | None = None
    ending_balance: Decimal | None = None
    section_totals: list[dict[str, Any]] = field(default_factory=list)
    column_totals: list[dict[str, Any]] = field(default_factory=list)
    principal_before: Decimal | None = None
    principal_after: Decimal | None = None
    principal_paid: Decimal | None = None
    interest: Decimal | None = None
    escrow: Decimal | None = None
    fees: Decimal | None = None
    advances: Decimal | None = None


@dataclass(frozen=True)
class LineRow:
    """One line of a clean account, as written."""

    ordinal: int
    posted_date: date
    description: str
    amount: Decimal
    page: int
    transaction_date: date | None = None
    running_balance: Decimal | None = None
    section: str | None = None


@dataclass(frozen=True)
class StatementChange:
    """One entry of the change feed: a statement at its current revision."""

    statement_id: UUID
    revision: int
    status: str
    document_type: str
    source_sha256: bytes
    change_seq: int


def new_statement_id(conn: psycopg.Connection[dict[str, Any]]) -> UUID:
    """A fresh statement ID, taken before its first revision is written."""
    row = conn.execute("SELECT uuidv7() AS id").fetchone()
    assert row is not None
    statement_id: UUID = row["id"]
    return statement_id


def insert_revision(
    conn: psycopg.Connection[dict[str, Any]],
    statement_id: UUID,
    revision: RevisionRow,
) -> tuple[UUID, int]:
    """Append a revision; return its ID and the number the counter gave it."""
    row = conn.execute(
        """\
        INSERT INTO receipt.statement_revisions (
            statement_id, origin, document_type, status,
            hold_reason, rejected_bound, masked_runs, period_start, period_end,
            printed_account_count, pages, llm_model, prompt_version,
            first_pass_version, input_tokens, output_tokens
        ) VALUES (
            %(statement_id)s, %(origin)s, %(document_type)s, %(status)s,
            %(hold_reason)s, %(rejected_bound)s, %(masked_runs)s,
            %(period_start)s, %(period_end)s, %(printed_account_count)s,
            %(pages)s, %(llm_model)s, %(prompt_version)s,
            %(first_pass_version)s, %(input_tokens)s, %(output_tokens)s
        )
        RETURNING id, revision
        """,
        {"statement_id": statement_id, **revision.__dict__},
    ).fetchone()
    assert row is not None
    return row["id"], row["revision"]


def insert_account(
    conn: psycopg.Connection[dict[str, Any]],
    revision_id: UUID,
    account: AccountRow,
) -> UUID:
    """Append one account to a revision; return its ID."""
    values = {
        **account.__dict__,
        "section_totals": Jsonb(account.section_totals),
        "column_totals": Jsonb(account.column_totals),
    }
    columns = list(values)
    row = conn.execute(
        "INSERT INTO receipt.statement_accounts (revision_id, "
        + ", ".join(columns)
        + ") VALUES (%(revision_id)s, "
        + ", ".join(f"%({c})s" for c in columns)
        + ") RETURNING id",
        {"revision_id": revision_id, **values},
    ).fetchone()
    assert row is not None
    account_id: UUID = row["id"]
    return account_id


def insert_lines(
    conn: psycopg.Connection[dict[str, Any]],
    account_id: UUID,
    lines: Sequence[LineRow],
) -> None:
    """Append a clean account's lines. A trigger refuses any other account."""
    with conn.cursor() as cur:
        cur.executemany(
            """\
            INSERT INTO receipt.statement_lines (
                account_id, ordinal, posted_date, transaction_date,
                description, amount, running_balance, section, page
            ) VALUES (
                %(account_id)s, %(ordinal)s, %(posted_date)s,
                %(transaction_date)s, %(description)s, %(amount)s,
                %(running_balance)s, %(section)s, %(page)s
            )
            """,
            [{"account_id": account_id, **line.__dict__} for line in lines],
        )


def create_statement(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    statement_id: UUID,
    original_file_id: UUID,
    source_sha256: bytes,
    revision_id: UUID,
    status: str,
    document_type: str,
) -> int:
    """Write a new statement pointing at its first revision; return change_seq."""
    row = conn.execute(
        """\
        INSERT INTO receipt.statements (
            id, original_file_id, source_sha256, current_revision_id,
            document_type, status
        ) VALUES (
            %(id)s, %(original_file_id)s, %(source_sha256)s, %(revision_id)s,
            %(document_type)s, %(status)s
        )
        RETURNING change_seq
        """,
        {
            "id": statement_id,
            "original_file_id": original_file_id,
            "source_sha256": source_sha256,
            "revision_id": revision_id,
            "document_type": document_type,
            "status": status,
        },
    ).fetchone()
    assert row is not None
    change_seq: int = row["change_seq"]
    return change_seq


def set_current_revision(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    statement_id: UUID,
    revision_id: UUID,
    status: str,
    document_type: str,
) -> int:
    """Point a statement at a new revision; return its new change_seq."""
    row = conn.execute(
        """\
        UPDATE receipt.statements
        SET current_revision_id = %(revision_id)s, status = %(status)s,
            document_type = %(document_type)s
        WHERE id = %(id)s
        RETURNING change_seq
        """,
        {
            "id": statement_id,
            "revision_id": revision_id,
            "status": status,
            "document_type": document_type,
        },
    ).fetchone()
    if row is None:
        msg = f"No statement {statement_id}"
        raise LookupError(msg)
    change_seq: int = row["change_seq"]
    return change_seq


def add_source(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    statement_id: UUID,
    source_name: str,
    source_id: str,
    file_name: str | None,
    received_at: datetime | None,
) -> None:
    """Record a source item that delivered a statement's bytes."""
    conn.execute(
        """\
        INSERT INTO receipt.statement_sources (
            statement_id, source_name, source_id, file_name, received_at
        ) VALUES (
            %(statement_id)s, %(source_name)s, %(source_id)s, %(file_name)s,
            %(received_at)s
        )
        """,
        {
            "statement_id": statement_id,
            "source_name": source_name,
            "source_id": source_id,
            "file_name": file_name,
            "received_at": received_at,
        },
    )


def republish(conn: psycopg.Connection[dict[str, Any]], statement_id: UUID) -> int:
    """Give a statement a new place in the feed, with no new revision.

    Increments ``publish_count``, which the trigger reads as a republish.
    Returns the new change_seq. The caller commits.
    """
    row = conn.execute(
        "UPDATE receipt.statements SET publish_count = publish_count + 1"
        " WHERE id = %s RETURNING change_seq",
        (statement_id,),
    ).fetchone()
    if row is None:
        msg = f"No statement {statement_id}"
        raise LookupError(msg)
    change_seq: int = row["change_seq"]
    return change_seq


def republish_all(conn: psycopg.Connection[dict[str, Any]]) -> int:
    """Republish every statement, committing after each one.

    One transaction per statement holds the feed lock for one statement at
    a time, so the worker isn't blocked for the whole run, and an
    interrupted run is safe to repeat. Returns how many were republished.
    """
    rows = conn.execute("SELECT id FROM receipt.statements ORDER BY id").fetchall()
    conn.commit()
    for row in rows:
        republish(conn, row["id"])
        conn.commit()
    return len(rows)


def changes_after(
    conn: psycopg.Connection[dict[str, Any]], after: int, limit: int
) -> list[StatementChange]:
    """Statements whose change_seq is above ``after``, oldest change first."""
    rows = conn.execute(
        """\
        SELECT s.id AS statement_id, r.revision, s.status, s.document_type,
               s.source_sha256, s.change_seq
        FROM receipt.statements s
        JOIN receipt.statement_revisions r ON r.id = s.current_revision_id
        WHERE s.change_seq > %s
        ORDER BY s.change_seq
        LIMIT %s
        """,
        (after, limit),
    ).fetchall()
    return [StatementChange(**row) for row in rows]
