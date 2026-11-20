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

Locks: drawing a counter value takes the feed lock (the counter's row) until
commit. A write that also updates a statement row takes the feed lock first
(:func:`set_current_revision` and :func:`republish` do), so two writers to
the same statement take the locks in one order and can't deadlock. Keep the
transaction short: no model call or PDF read between the first write and
the commit, since every other statement writer waits on the feed lock.
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


def lock_feed(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Take the feed lock, held until commit, before any statement row lock."""
    conn.execute("SELECT receipt.lock_statement_feed()")


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
    row = conn.execute(
        """\
        INSERT INTO receipt.statement_accounts (
            revision_id, ordinal, occurrence, institution, last4, account_type,
            currency, period_start, period_end, status, held_by_statement,
            failure_class, difference, first_page, last_page, line_order,
            date_grace_days, beginning_balance, ending_balance, section_totals,
            column_totals, principal_before, principal_after, principal_paid,
            interest, escrow, fees, advances, content_digest
        ) VALUES (
            %(revision_id)s, %(ordinal)s, %(occurrence)s, %(institution)s,
            %(last4)s, %(account_type)s, %(currency)s, %(period_start)s,
            %(period_end)s, %(status)s, %(held_by_statement)s,
            %(failure_class)s, %(difference)s, %(first_page)s, %(last_page)s,
            %(line_order)s, %(date_grace_days)s, %(beginning_balance)s,
            %(ending_balance)s, %(section_totals)s, %(column_totals)s,
            %(principal_before)s, %(principal_after)s, %(principal_paid)s,
            %(interest)s, %(escrow)s, %(fees)s, %(advances)s,
            %(content_digest)s
        )
        RETURNING id
        """,
        {
            "revision_id": revision_id,
            **account.__dict__,
            "section_totals": Jsonb(account.section_totals),
            "column_totals": Jsonb(account.column_totals),
        },
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
    status: StatementStatus,
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
    status: StatementStatus,
    document_type: str,
) -> int:
    """Point a statement at a new revision; return its new change_seq."""
    lock_feed(conn)
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


def statement_for_sha256(
    conn: psycopg.Connection[dict[str, Any]], sha256: bytes
) -> UUID | None:
    """The statement already made from these bytes, if any.

    The same PDF dropped twice is one statement with two source rows.
    """
    row = conn.execute(
        "SELECT id FROM receipt.statements WHERE source_sha256 = %(sha256)s",
        {"sha256": sha256},
    ).fetchone()
    return None if row is None else row["id"]


def statement_source_exists(
    conn: psycopg.Connection[dict[str, Any]], source_id: str
) -> bool:
    """Whether a source item has already delivered a statement."""
    row = conn.execute(
        "SELECT 1 FROM receipt.statement_sources WHERE source_id = %(source_id)s",
        {"source_id": source_id},
    ).fetchone()
    return row is not None


def add_source(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    statement_id: UUID,
    source_name: str,
    source_id: str,
    file_name: str | None,
    received_at: datetime | None,
) -> None:
    """Record a source item that delivered a statement's bytes.

    Takes the feed lock first: the insert's foreign key locks the statement
    row, and a republish of it waits on that.
    """
    lock_feed(conn)
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
    lock_feed(conn)
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


def republish_each(
    conn: psycopg.Connection[dict[str, Any]], statement_ids: Sequence[UUID]
) -> list[int]:
    """Republish each statement, committing after each one.

    One transaction per statement holds the feed lock for one statement at
    a time, so the worker isn't blocked for the whole run, and an
    interrupted run is safe to repeat. Returns each one's new change_seq,
    in order. A statement that doesn't exist raises ``LookupError``, after
    the ones before it have been committed.
    """
    conn.commit()
    new_seqs: list[int] = []
    for statement_id in statement_ids:
        new_seqs.append(republish(conn, statement_id))
        conn.commit()
    return new_seqs


def all_statement_ids(conn: psycopg.Connection[dict[str, Any]]) -> list[UUID]:
    """Every statement's ID, in ID order."""
    rows = conn.execute("SELECT id FROM receipt.statements ORDER BY id").fetchall()
    return [row["id"] for row in rows]


def republish_all(conn: psycopg.Connection[dict[str, Any]]) -> int:
    """Republish every statement, committing after each one.

    See :func:`republish_each`. Returns how many were republished.
    """
    return len(republish_each(conn, all_statement_ids(conn)))


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


# ---------------------------------------------------------------------------
# Reads for the MCP statement tools (plan U7)
# ---------------------------------------------------------------------------


def statement_exists(
    conn: psycopg.Connection[dict[str, Any]], statement_id: UUID
) -> bool:
    """Whether a statement with this ID exists."""
    row = conn.execute(
        "SELECT 1 FROM receipt.statements WHERE id = %s", (statement_id,)
    ).fetchone()
    return row is not None


def get_revision(
    conn: psycopg.Connection[dict[str, Any]],
    statement_id: UUID,
    revision: int | None = None,
) -> dict[str, Any] | None:
    """One revision of a statement, or its current one when ``revision`` is None.

    The row carries the revision's columns, its ``revision_id``, the
    statement's ``source_sha256`` and ``current_revision``, and
    ``is_current``. None when the statement or that revision doesn't exist.
    """
    match = "r.id = s.current_revision_id" if revision is None else "r.revision = %s"
    params: tuple[Any, ...] = (
        (statement_id,) if revision is None else (statement_id, revision)
    )
    return conn.execute(
        f"""\
        SELECT s.id AS statement_id, s.source_sha256,
               cr.revision AS current_revision,
               r.id = s.current_revision_id AS is_current,
               r.id AS revision_id, r.revision, r.document_type, r.status,
               r.hold_reason, r.rejected_bound, r.masked_runs, r.period_start,
               r.period_end, r.printed_account_count, r.pages, r.llm_model,
               r.prompt_version, r.first_pass_version, r.created_at, r.origin,
               r.input_tokens, r.output_tokens
        FROM receipt.statements s
        JOIN receipt.statement_revisions cr ON cr.id = s.current_revision_id
        JOIN receipt.statement_revisions r ON r.statement_id = s.id
        WHERE s.id = %s AND {match}
        """,
        params,
    ).fetchone()


_ACCOUNT_COLUMNS = """\
a.id AS account_id, a.ordinal, a.occurrence, a.institution, a.last4,
a.account_type, a.currency, a.period_start, a.period_end, a.status,
a.held_by_statement, a.failure_class, a.difference, a.first_page,
a.last_page, a.line_order, a.date_grace_days, a.beginning_balance,
a.ending_balance, a.section_totals, a.column_totals, a.principal_before,
a.principal_after, a.principal_paid, a.interest, a.escrow, a.fees,
a.advances, a.content_digest,
(SELECT count(*) FROM receipt.statement_lines l WHERE l.account_id = a.id)
    AS line_count"""


def get_accounts(
    conn: psycopg.Connection[dict[str, Any]], revision_id: UUID
) -> list[dict[str, Any]]:
    """A revision's accounts in document order, each with its ``line_count``."""
    return conn.execute(
        f"SELECT {_ACCOUNT_COLUMNS} FROM receipt.statement_accounts a"
        " WHERE a.revision_id = %s ORDER BY a.ordinal",
        (revision_id,),
    ).fetchall()


def get_account(
    conn: psycopg.Connection[dict[str, Any]], revision_id: UUID, ordinal: int
) -> dict[str, Any] | None:
    """One account of a revision by its ordinal, or None."""
    return conn.execute(
        f"SELECT {_ACCOUNT_COLUMNS} FROM receipt.statement_accounts a"
        " WHERE a.revision_id = %s AND a.ordinal = %s",
        (revision_id, ordinal),
    ).fetchone()


def get_lines(
    conn: psycopg.Connection[dict[str, Any]],
    account_id: UUID,
    *,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """One page of an account's lines, in ``ordinal`` order."""
    return conn.execute(
        """\
        SELECT id AS line_id, ordinal, posted_date, transaction_date,
               description, amount, running_balance, section, page
        FROM receipt.statement_lines
        WHERE account_id = %s
        ORDER BY ordinal
        LIMIT %s OFFSET %s
        """,
        (account_id, limit, offset),
    ).fetchall()


# ---------------------------------------------------------------------------
# Reads for the statements CLI (plan U8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementListing:
    """One statement at its current revision, for ``statements list``."""

    change: StatementChange
    extracted_at: datetime
    hold_reason: str | None


def list_statements(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    statuses: Sequence[str] = (),
    since: datetime | None = None,
) -> list[StatementListing]:
    """Statements at their current revision, in feed order.

    ``statuses`` keeps only those statuses (none: all); ``since`` keeps
    current revisions extracted at or after it.
    """
    rows = conn.execute(
        """        SELECT s.id AS statement_id, r.revision, s.status, s.document_type,
               s.source_sha256, s.change_seq, r.created_at AS extracted_at,
               r.hold_reason
        FROM receipt.statements s
        JOIN receipt.statement_revisions r ON r.id = s.current_revision_id
        WHERE (cardinality(%(statuses)s::text[]) = 0
               OR s.status = ANY(%(statuses)s::text[]))
          AND (%(since)s::timestamptz IS NULL OR r.created_at >= %(since)s)
        ORDER BY s.change_seq
        """,
        {"statuses": list(statuses), "since": since},
    ).fetchall()
    return [
        StatementListing(
            change=StatementChange(
                statement_id=row["statement_id"],
                revision=row["revision"],
                status=row["status"],
                document_type=row["document_type"],
                source_sha256=row["source_sha256"],
                change_seq=row["change_seq"],
            ),
            extracted_at=row["extracted_at"],
            hold_reason=row["hold_reason"],
        )
        for row in rows
    ]


def original_location(
    conn: psycopg.Connection[dict[str, Any]], statement_id: UUID
) -> dict[str, Any] | None:
    """Where a statement's original is stored, or None for no statement.

    The row has the statement's ``source_sha256`` and the store's ``ref``
    (``backend:locator``) of its original, or a null ``ref`` when the store
    holds no copy of it.
    """
    row = conn.execute(
        """        SELECT s.source_sha256,
               (SELECT l.backend || ':' || l.locator
                  FROM receipt.file_locations l
                 WHERE l.file_id = s.original_file_id
                   AND l.purpose = 'original' AND l.owner = 'store'
                 ORDER BY l.created_at, l.id
                 LIMIT 1) AS ref
        FROM receipt.statements s
        WHERE s.id = %s
        """,
        (statement_id,),
    ).fetchone()
    return row


def feed_positions(
    conn: psycopg.Connection[dict[str, Any]], statement_ids: Sequence[UUID] | None
) -> list[dict[str, Any]]:
    """Each statement's current revision and place in the feed, in ID order.

    ``None`` selects every statement; IDs that don't exist are left out.
    """
    return conn.execute(
        """        SELECT s.id AS statement_id, r.revision, s.change_seq
        FROM receipt.statements s
        JOIN receipt.statement_revisions r ON r.id = s.current_revision_id
        WHERE %(all)s OR s.id = ANY(%(ids)s)
        ORDER BY s.id
        """,
        {"all": statement_ids is None, "ids": list(statement_ids or [])},
    ).fetchall()
