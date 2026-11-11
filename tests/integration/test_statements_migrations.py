"""Migrations 000023 and 000024: statements, revisions, accounts, lines and
the feed counter.

Runs the real migration files, grants included, in a scratch database (the
``scratch`` fixture from the 000011 tests, which creates and drops it), so it
never touches the shared ``receipt_index_dev`` schema. Every statement here is
synthetic.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from receipt_index import statement_repository as repo
from receipt_index.statement_repository import AccountRow, LineRow, RevisionRow

from .test_extractions_migrations import (  # noqa: F401 (scratch is a fixture)
    _ADMIN_DSN,
    _migration,
    _run,
    scratch,
)

pytestmark = pytest.mark.integration

Scratch = tuple[psycopg.Connection[dict[str, Any]], str]
Conn = psycopg.Connection[dict[str, Any]]

_CLEAN = RevisionRow(origin="ingest", document_type="deposit", status="complete")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect(conn: Conn) -> Conn:
    """Another connection to the same scratch database, as the superuser."""
    dsn = _ADMIN_DSN.rsplit("/", 1)[0] + f"/{conn.info.dbname}"
    return psycopg.connect(dsn, row_factory=dict_row)


def _exists(conn: Conn, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS r", (name,)).fetchone()
    return row is not None and row["r"] is not None


def _feed_row(conn: Conn) -> Any:
    return conn.execute("SELECT * FROM receipt.statement_feed_state").fetchall()


def _stored_file(conn: Conn) -> tuple[Any, bytes]:
    """A stored original, committed. Returns its ID and SHA-256."""
    sha = hashlib.sha256(uuid.uuid4().bytes).digest()
    row = conn.execute(
        "INSERT INTO receipt.stored_files (sha256, byte_size, media_type)"
        " VALUES (%s, 1024, 'application/pdf') RETURNING id",
        (sha,),
    ).fetchone()
    assert row is not None
    conn.commit()
    return row["id"], sha


def _digest() -> bytes:
    return hashlib.sha256(uuid.uuid4().bytes).digest()


def _account(**changes: Any) -> AccountRow:
    values: dict[str, Any] = {
        "ordinal": 1,
        "occurrence": 1,
        "account_type": "deposit",
        "status": "clean",
        "content_digest": _digest(),
        "institution": "Synthetic Savings Bank",
        "last4": "1234",
        "currency": "USD",
        "beginning_balance": Decimal("100.00"),
        "ending_balance": Decimal("90.00"),
        "line_order": "oldest_first",
    }
    values.update(changes)
    return AccountRow(**values)


def _line(ordinal: int = 1, **changes: Any) -> LineRow:
    values: dict[str, Any] = {
        "ordinal": ordinal,
        "posted_date": date(2026, 1, 5),
        "description": "Synthetic card purchase",
        "amount": Decimal("-10.00"),
        "page": 1,
    }
    values.update(changes)
    return LineRow(**values)


def _new_statement(
    conn: Conn,
    revision: RevisionRow = _CLEAN,
    *,
    with_lines: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    """A new statement in the plan's insert order. Commits unless told not to."""
    file_id, sha = _stored_file(conn)
    statement_id = repo.new_statement_id(conn)
    revision_id, number = repo.insert_revision(conn, statement_id, revision)
    if revision.status != "rejected":
        account_id = repo.insert_account(conn, revision_id, _account())
        if with_lines:
            repo.insert_lines(conn, account_id, [_line(1), _line(2)])
    change_seq = repo.create_statement(
        conn,
        statement_id=statement_id,
        original_file_id=file_id,
        source_sha256=sha,
        revision_id=revision_id,
        status=revision.status,
        document_type=revision.document_type,
    )
    repo.add_source(
        conn,
        statement_id=statement_id,
        source_name="statements-drive",
        source_id=f"drive-{uuid.uuid4().hex}",
        file_name="synthetic-statement.pdf",
        received_at=None,
    )
    if commit:
        conn.commit()
    return {
        "id": statement_id,
        "revision_id": revision_id,
        "revision": number,
        "change_seq": change_seq,
    }


def _open_revision(conn: Conn, revision: RevisionRow = _CLEAN) -> Any:
    """A revision written in the open transaction, so it still takes accounts.

    Its statement row is never written; the caller rolls back.
    """
    revision_id, _ = repo.insert_revision(conn, repo.new_statement_id(conn), revision)
    return revision_id


def _statement(conn: Conn, statement_id: Any) -> Any:
    return conn.execute(
        "SELECT *, xmin::text AS xmin FROM receipt.statements WHERE id = %s",
        (statement_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Migrations up and down
# ---------------------------------------------------------------------------

_TABLES = (
    "receipt.statement_feed_state",
    "receipt.statements",
    "receipt.statement_revisions",
    "receipt.statement_sources",
    "receipt.statement_accounts",
    "receipt.statement_lines",
)


_FUNCTIONS = (
    "lock_statement_feed",
    "next_feed_value",
    "forbid_statement_history_change",
    "set_statement_revision_number",
    "set_statement_change_seq",
    "check_statement_current_revision",
    "check_statement_account_revision",
    "check_statement_line_account",
)


def test_up_down_up_with_the_counter_seeded(scratch: Scratch) -> None:  # noqa: F811
    conn, _ = scratch
    assert all(_exists(conn, t) for t in _TABLES)
    assert _feed_row(conn) == [{"id": True, "last_value": 0}]
    _new_statement(conn)

    _run(conn, _migration("000024", "down"))
    _run(conn, _migration("000023", "down"))
    assert not any(_exists(conn, t) for t in _TABLES)
    left = conn.execute(
        "SELECT proname FROM pg_proc WHERE pronamespace = 'receipt'::regnamespace"
        " AND proname = ANY(%s)",
        (list(_FUNCTIONS),),
    ).fetchall()
    assert left == []

    _run(conn, _migration("000023", "up"))
    _run(conn, _migration("000024", "up"))
    assert all(_exists(conn, t) for t in _TABLES)
    assert _feed_row(conn) == [{"id": True, "last_value": 0}]
    # And it works again.
    assert _new_statement(conn)["revision"] > 0


def test_the_counter_has_one_row(scratch: Scratch) -> None:  # noqa: F811
    conn, _ = scratch
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO receipt.statement_feed_state (id, last_value)"
            " VALUES (FALSE, 0)"
        )
    conn.rollback()


# ---------------------------------------------------------------------------
# Insert order, and the statement's copy of its revision
# ---------------------------------------------------------------------------


def test_a_new_statement_and_a_rerun_commit_in_the_insert_order(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    first = _new_statement(conn)
    assert first["revision"] > 0
    assert first["change_seq"] > first["revision"]

    rerun = RevisionRow(
        origin="rerun",
        document_type="deposit",
        status="held",
        hold_reason="no_usable_account",
    )
    revision_id, number = repo.insert_revision(conn, first["id"], rerun)
    repo.insert_account(
        conn,
        revision_id,
        _account(
            status="held", failure_class="does_not_foot", difference=Decimal("1.00")
        ),
    )
    change_seq = repo.set_current_revision(
        conn,
        statement_id=first["id"],
        revision_id=revision_id,
        status="held",
        document_type="deposit",
    )
    conn.commit()
    assert number > first["change_seq"]
    assert change_seq > number
    row = _statement(conn, first["id"])
    assert row["current_revision_id"] == revision_id
    assert row["status"] == "held"


def test_a_status_that_doesnt_match_the_revision_fails_at_commit(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    file_id, sha = _stored_file(conn)
    statement_id = repo.new_statement_id(conn)
    revision_id, _ = repo.insert_revision(conn, statement_id, _CLEAN)
    repo.create_statement(
        conn,
        statement_id=statement_id,
        original_file_id=file_id,
        source_sha256=sha,
        revision_id=revision_id,
        status="held",  # the revision says complete
        document_type="deposit",
    )
    with pytest.raises(psycopg.errors.RaiseException, match="don't match"):
        conn.commit()


@pytest.mark.parametrize(
    ("status", "document_type"),
    [("held", "deposit"), ("complete", "card")],
    ids=["status", "document-type"],
)
def test_a_rerun_that_doesnt_match_its_revision_fails_at_commit(
    scratch: Scratch,  # noqa: F811
    status: Any,
    document_type: str,
) -> None:
    conn, _ = scratch
    made = _new_statement(conn)
    revision_id, _ = repo.insert_revision(conn, made["id"], _CLEAN)
    repo.set_current_revision(
        conn,
        statement_id=made["id"],
        revision_id=revision_id,
        status=status,
        document_type=document_type,
    )
    with pytest.raises(psycopg.errors.RaiseException, match="don't match"):
        conn.commit()


def test_a_revision_of_another_statement_fails_at_commit(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    other = _new_statement(conn)
    file_id, sha = _stored_file(conn)
    repo.create_statement(
        conn,
        statement_id=repo.new_statement_id(conn),
        original_file_id=file_id,
        source_sha256=sha,
        revision_id=other["revision_id"],
        status="complete",
        document_type="deposit",
    )
    with pytest.raises(psycopg.errors.RaiseException, match="belongs to statement"):
        conn.commit()


def test_a_revision_with_no_statement_fails_at_commit(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    repo.insert_revision(conn, uuid.uuid4(), _CLEAN)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.commit()


def test_a_statement_with_no_revision_fails_at_commit(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    file_id, sha = _stored_file(conn)
    repo.create_statement(
        conn,
        statement_id=repo.new_statement_id(conn),
        original_file_id=file_id,
        source_sha256=sha,
        revision_id=uuid.uuid4(),
        status="complete",
        document_type="deposit",
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.commit()


# ---------------------------------------------------------------------------
# The counter: revision numbers and change_seq
# ---------------------------------------------------------------------------


def test_revision_and_change_seq_ignore_the_values_given(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    made = _new_statement(conn)
    conn.execute(
        "UPDATE receipt.statements SET change_seq = 1 WHERE id = %s", (made["id"],)
    )
    conn.commit()
    assert _statement(conn, made["id"])["change_seq"] == made["change_seq"]
    file_id, sha = _stored_file(conn)
    statement_id = repo.new_statement_id(conn)
    row = conn.execute(
        "INSERT INTO receipt.statement_revisions (statement_id, revision, origin,"
        " document_type, status) VALUES (%s, 5, 'ingest', 'deposit', 'complete')"
        " RETURNING id, revision",
        (statement_id,),
    ).fetchone()
    assert row is not None
    assert row["revision"] > made["change_seq"]
    conn.execute(
        "INSERT INTO receipt.statements (id, original_file_id, source_sha256,"
        " current_revision_id, document_type, status, change_seq, publish_count)"
        " VALUES (%s, %s, %s, %s, 'deposit', 'complete', 7, 9)",
        (statement_id, file_id, sha, row["id"]),
    )
    conn.commit()
    stored = _statement(conn, statement_id)
    assert stored["change_seq"] > row["revision"]
    assert stored["publish_count"] == 0


def test_an_update_that_moves_nothing_keeps_change_seq(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    made = _new_statement(conn)
    conn.execute(
        "UPDATE receipt.statements SET status = status, updated_at = now()"
        " WHERE id = %s",
        (made["id"],),
    )
    conn.commit()
    assert _statement(conn, made["id"])["change_seq"] == made["change_seq"]


def test_publish_count_only_goes_up_and_identity_never_changes(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    made = _new_statement(conn)
    repo.republish(conn, made["id"])
    conn.commit()
    for change in (
        "publish_count = 0",
        "publish_count = publish_count + 2",
        "source_sha256 = sha256('x')",
        "original_file_id = uuidv7()",
    ):
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                f"UPDATE receipt.statements SET {change} WHERE id = %s",
                (made["id"],),
            )
        conn.rollback()


def test_the_counter_stays_monotonic_when_set_back(
    scratch: Scratch,  # noqa: F811
) -> None:
    # A restore from an older backup brings back a lower last value.
    conn, _ = scratch
    before = _new_statement(conn)
    conn.execute("UPDATE receipt.statement_feed_state SET last_value = 1")
    conn.commit()
    after = _new_statement(conn)
    assert after["revision"] > before["change_seq"]
    assert after["change_seq"] > after["revision"]


def test_the_counter_counts_on_when_it_is_ahead_of_the_clock(
    scratch: Scratch,  # noqa: F811
) -> None:
    # Values handed out ahead of the clock (a restore from a host with a fast
    # clock, or a clock stepped back) are never repeated.
    conn, _ = scratch
    row = conn.execute(
        "SELECT (extract(epoch FROM clock_timestamp()) * 1000000)::bigint"
        " + 1000000000000 AS ahead"
    ).fetchone()
    assert row is not None
    ahead = row["ahead"]
    conn.execute("UPDATE receipt.statement_feed_state SET last_value = %s", (ahead,))
    conn.commit()
    made = _new_statement(conn)
    assert made["revision"] == ahead + 1
    assert made["change_seq"] == ahead + 2


def test_republish_moves_a_statement_in_the_feed_without_a_revision(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    made = _new_statement(conn)
    seq = repo.republish(conn, made["id"])
    conn.commit()
    row = _statement(conn, made["id"])
    assert seq > made["change_seq"]
    assert row["change_seq"] == seq
    assert row["publish_count"] == 1
    assert row["current_revision_id"] == made["revision_id"]
    count = conn.execute(
        "SELECT count(*) AS n FROM receipt.statement_revisions WHERE statement_id = %s",
        (made["id"],),
    ).fetchone()
    assert count is not None
    assert count["n"] == 1


def test_a_restored_row_below_the_cursor_reappears_after_republish(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    restored = _new_statement(conn)
    later = _new_statement(conn)
    cursor = later["change_seq"]
    # What a restore does in effect: the row comes back with an old
    # change_seq, below a cursor the ledger already holds.
    conn.execute(
        "ALTER TABLE receipt.statements DISABLE TRIGGER trg_statements_change_seq"
    )
    conn.execute(
        "UPDATE receipt.statements SET change_seq = 1 WHERE id = %s", (restored["id"],)
    )
    conn.commit()  # the deferred check fires before the trigger is re-enabled
    conn.execute(
        "ALTER TABLE receipt.statements ENABLE TRIGGER trg_statements_change_seq"
    )
    conn.commit()
    assert repo.changes_after(conn, cursor, 100) == []

    repo.republish(conn, restored["id"])
    conn.commit()
    (change,) = repo.changes_after(conn, cursor, 100)
    assert change.statement_id == restored["id"]
    assert change.revision == restored["revision"]


def test_republish_all_commits_once_per_statement(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    made = [_new_statement(conn) for _ in range(3)]
    assert repo.republish_all(conn) == 3
    rows = [_statement(conn, m["id"]) for m in made]
    assert all(r["publish_count"] == 1 for r in rows)
    # Each row was last written by its own transaction.
    assert len({r["xmin"] for r in rows}) == 3
    seqs = [r["change_seq"] for r in rows]
    assert all(s > max(m["change_seq"] for m in made) for s in seqs)


def test_a_late_commit_is_never_skipped(scratch: Scratch) -> None:  # noqa: F811
    # The cursor guarantee: writer A draws its numbers and stops before
    # committing; writer B blocks on the counter's lock until A commits. A
    # reader in between sees neither, then sees A before B.
    conn, _ = scratch
    writer_a, writer_b, reader = _connect(conn), _connect(conn), _connect(conn)
    try:
        a = _new_statement(writer_a, commit=False)  # holds the advisory lock
        b_result: dict[str, Any] = {}
        b_started = threading.Event()

        def write_b() -> None:
            b_started.set()
            try:
                b_result.update(_new_statement(writer_b))
            except Exception as exc:  # surfaced below, not lost in the thread
                b_result["error"] = exc

        # B commits its stored file first, then blocks drawing its revision
        # number, on the lock A holds.
        thread = threading.Thread(target=write_b)
        thread.start()
        b_started.wait(5)
        _wait_for_lock_wait(reader, writer_b.info.backend_pid)
        assert repo.changes_after(reader, 0, 100) == []
        reader.commit()

        writer_a.commit()
        thread.join(30)
        assert not thread.is_alive()
        assert "error" not in b_result, b_result.get("error")

        changes = repo.changes_after(reader, 0, 100)
        assert [c.statement_id for c in changes] == [a["id"], b_result["id"]]
        assert changes[0].change_seq < changes[1].change_seq
        assert b_result["revision"] > a["change_seq"]
    finally:
        for c in (writer_a, writer_b, reader):
            c.close()


def _wait_for_lock_wait(conn: Conn, pid: int) -> None:
    """Wait until backend ``pid`` is blocked waiting for a lock."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT 1 AS waiting FROM pg_stat_activity WHERE pid = %s"
            " AND wait_event_type = 'Lock'",
            (pid,),
        ).fetchone()
        conn.commit()
        if row is not None:
            return
        time.sleep(0.05)
    pytest.fail(f"backend {pid} never waited on a lock")


def test_a_republish_beside_a_rerun_of_the_same_statement_doesnt_deadlock(
    scratch: Scratch,  # noqa: F811
) -> None:
    # The re-run holds the feed lock from its revision insert; the republish
    # must wait for it before it locks the statement row, so the re-run can
    # still take that row. Without the shared lock order this deadlocks.
    conn, _ = scratch
    made = _new_statement(conn)
    worker, operator = _connect(conn), _connect(conn)
    try:
        revision_id, _ = repo.insert_revision(worker, made["id"], _CLEAN)
        result: dict[str, Any] = {}

        def republish() -> None:
            try:
                result["seq"] = repo.republish(operator, made["id"])
                operator.commit()
            except Exception as exc:  # surfaced below
                result["error"] = exc
                operator.rollback()

        thread = threading.Thread(target=republish)
        thread.start()
        _wait_for_lock_wait(conn, operator.info.backend_pid)
        conn.commit()
        rerun_seq = repo.set_current_revision(
            worker,
            statement_id=made["id"],
            revision_id=revision_id,
            status="complete",
            document_type="deposit",
        )
        worker.commit()
        thread.join(30)
        assert not thread.is_alive()
        assert "error" not in result, result.get("error")
        assert result["seq"] > rerun_seq
        row = _statement(conn, made["id"])
        assert row["current_revision_id"] == revision_id
        assert row["publish_count"] == 1
    finally:
        worker.close()
        operator.close()


# ---------------------------------------------------------------------------
# Accounts and lines
# ---------------------------------------------------------------------------


def test_a_line_cant_attach_to_a_held_or_loan_account(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    for account in (
        _account(ordinal=2, status="held", failure_class="does_not_foot"),
        _account(
            ordinal=3, account_type="loan", status="figures_only", line_order=None
        ),
    ):
        account_id = repo.insert_account(conn, _open_revision(conn), account)
        with pytest.raises(psycopg.errors.RaiseException, match="carries no lines"):
            repo.insert_lines(conn, account_id, [_line()])
        conn.rollback()


def test_a_rejected_revision_has_no_accounts(scratch: Scratch) -> None:  # noqa: F811
    conn, _ = scratch
    rejected = RevisionRow(origin="ingest", document_type="scan", status="rejected")
    revision_id = _open_revision(conn, rejected)
    with pytest.raises(psycopg.errors.RaiseException, match="rejection"):
        repo.insert_account(conn, revision_id, _account())
    conn.rollback()


def test_a_committed_revision_takes_no_more_accounts_or_lines(
    scratch: Scratch,  # noqa: F811
) -> None:
    # The feed has published it: a later write must not change it in place.
    conn, _ = scratch
    made = _new_statement(conn)
    account = conn.execute(
        "SELECT id FROM receipt.statement_accounts WHERE revision_id = %s",
        (made["revision_id"],),
    ).fetchone()
    assert account is not None
    with pytest.raises(psycopg.errors.RaiseException, match="takes no more accounts"):
        repo.insert_account(conn, made["revision_id"], _account(ordinal=2))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="takes no more lines"):
        repo.insert_lines(conn, account["id"], [_line(3)])
    conn.rollback()


def test_a_revisions_accounts_may_follow_a_savepoint(
    scratch: Scratch,  # noqa: F811
) -> None:
    # Within its own transaction a revision takes accounts and lines, even
    # after a savepoint (a nested transaction block).
    conn, _ = scratch
    file_id, sha = _stored_file(conn)
    statement_id = repo.new_statement_id(conn)
    revision_id, _ = repo.insert_revision(conn, statement_id, _CLEAN)
    with conn.transaction():
        account_id = repo.insert_account(conn, revision_id, _account())
        with conn.transaction():
            repo.insert_lines(conn, account_id, [_line()])
    repo.create_statement(
        conn,
        statement_id=statement_id,
        original_file_id=file_id,
        source_sha256=sha,
        revision_id=revision_id,
        status="complete",
        document_type="deposit",
    )
    conn.commit()


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"last4": "12345"}, "ck_statement_accounts_last4"),
        ({"last4": "12a4"}, "ck_statement_accounts_last4"),
        ({"currency": "usd"}, "ck_statement_accounts_currency"),
        ({"currency": "US"}, "ck_statement_accounts_currency"),
        ({"status": "figures_only"}, "ck_statement_accounts_status"),
        ({"account_type": "loan"}, "ck_statement_accounts_status"),
        ({"status": "held"}, "ck_statement_accounts_held_has_reason"),
        ({"failure_class": "does_not_foot"}, "ck_statement_accounts_held_has_reason"),
        ({"held_by_statement": True}, "ck_statement_accounts_held_by_statement"),
        ({"interest": Decimal("1.00")}, "ck_statement_accounts_loan_figures"),
        ({"first_page": 3, "last_page": 2}, "ck_statement_accounts_pages"),
        ({"content_digest": b"short"}, "ck_statement_accounts_content_digest"),
        (
            {"section_totals": {"not": "a list"}},
            "ck_statement_accounts_totals_are_arrays",
        ),
    ],
)
def test_an_account_refuses_what_the_contract_rules_out(
    scratch: Scratch,  # noqa: F811
    changes: dict[str, Any],
    constraint: str,
) -> None:
    conn, _ = scratch
    revision_id = _open_revision(conn)
    with pytest.raises(psycopg.errors.CheckViolation) as raised:
        repo.insert_account(conn, revision_id, _account(ordinal=2, **changes))
    assert raised.value.diag.constraint_name == constraint
    conn.rollback()


@pytest.mark.parametrize(
    ("revision", "constraint"),
    [
        (
            RevisionRow(origin="ingest", document_type="scan", status="complete"),
            "ck_statement_revisions_rejected",
        ),
        (
            RevisionRow(origin="ingest", document_type="deposit", status="rejected"),
            "ck_statement_revisions_rejected",
        ),
        (
            RevisionRow(origin="ingest", document_type="deposit", status="held"),
            "ck_statement_revisions_held_has_reason",
        ),
        (
            RevisionRow(
                origin="ingest",
                document_type="over_bounds",
                status="rejected",
            ),
            "ck_statement_revisions_over_bounds_has_bound",
        ),
        (
            RevisionRow(
                origin="ingest",
                document_type="deposit",
                status="complete",
                period_start=date(2026, 2, 1),
                period_end=date(2026, 1, 1),
            ),
            "ck_statement_revisions_period",
        ),
    ],
)
def test_a_revision_refuses_what_the_contract_rules_out(
    scratch: Scratch,  # noqa: F811
    revision: RevisionRow,
    constraint: str,
) -> None:
    conn, _ = scratch
    with pytest.raises(psycopg.errors.CheckViolation) as raised:
        repo.insert_revision(conn, uuid.uuid4(), revision)
    assert raised.value.diag.constraint_name == constraint
    conn.rollback()


@pytest.mark.parametrize(
    ("table", "statement"),
    [
        ("statement_revisions", "UPDATE receipt.statement_revisions SET pages = 2"),
        ("statement_revisions", "DELETE FROM receipt.statement_revisions"),
        ("statement_accounts", "UPDATE receipt.statement_accounts SET ordinal = 9"),
        ("statement_lines", "DELETE FROM receipt.statement_lines"),
        ("statement_lines", "TRUNCATE receipt.statement_lines"),
        ("statement_revisions", "TRUNCATE receipt.statement_revisions CASCADE"),
        ("statement_accounts", "TRUNCATE receipt.statement_accounts CASCADE"),
    ],
)
def test_history_is_append_only_even_for_the_owner(
    scratch: Scratch,  # noqa: F811
    table: str,
    statement: str,
) -> None:
    conn, _ = scratch
    _new_statement(conn)
    with pytest.raises(
        psycopg.errors.RaiseException, match=rf"receipt\.{table} is not allowed"
    ):
        conn.execute(statement)
    conn.rollback()


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


def test_the_write_role_can_write_a_statement_but_not_the_counter(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    file_id, sha = _stored_file(conn)
    conn.execute("SET ROLE receipt_index_dev_write")
    statement_id = repo.new_statement_id(conn)
    revision_id, _ = repo.insert_revision(conn, statement_id, _CLEAN)
    account_id = repo.insert_account(conn, revision_id, _account())
    repo.insert_lines(conn, account_id, [_line()])
    repo.create_statement(
        conn,
        statement_id=statement_id,
        original_file_id=file_id,
        source_sha256=sha,
        revision_id=revision_id,
        status="complete",
        document_type="deposit",
    )
    repo.add_source(
        conn,
        statement_id=statement_id,
        source_name="statements-drive",
        source_id="drive-write-role",
        file_name=None,
        received_at=None,
    )
    repo.republish(conn, statement_id)
    conn.commit()
    # A re-run as the write role: a second revision, made current.
    conn.execute("SET ROLE receipt_index_dev_write")
    rerun_id, _ = repo.insert_revision(conn, statement_id, _CLEAN)
    repo.insert_account(conn, rerun_id, _account())
    repo.set_current_revision(
        conn,
        statement_id=statement_id,
        revision_id=rerun_id,
        status="complete",
        document_type="deposit",
    )
    conn.commit()
    conn.execute("SET ROLE receipt_index_dev_write")
    for statement in (
        "UPDATE receipt.statement_feed_state SET last_value = 1",
        "SELECT last_value FROM receipt.statement_feed_state",
        "UPDATE receipt.statements SET change_seq = 1",
        "UPDATE receipt.statements SET updated_at = now()",
        "INSERT INTO receipt.statement_sources (statement_id, source_name,"
        " source_id, created_at) SELECT id, 'x', 'y', now()"
        " FROM receipt.statements LIMIT 1",
        "UPDATE receipt.statements SET source_sha256 = sha256('x')",
        "DELETE FROM receipt.statement_sources",
        "UPDATE receipt.statement_sources SET file_name = 'x'",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()
        conn.execute("SET ROLE receipt_index_dev_write")
    conn.execute("RESET ROLE")
    conn.commit()


def test_the_read_role_reads_every_statement_table_and_writes_none(
    scratch: Scratch,  # noqa: F811
) -> None:
    conn, _ = scratch
    _new_statement(conn)
    conn.execute("SET ROLE receipt_index_dev_read")
    for table in _TABLES:
        conn.execute(f"SELECT count(*) FROM {table}")
    for statement in (
        "INSERT INTO receipt.statement_sources (statement_id, source_name,"
        " source_id) SELECT id, 'x', 'y' FROM receipt.statements LIMIT 1",
        "UPDATE receipt.statements SET publish_count = publish_count + 1",
        "SELECT receipt.next_feed_value()",
        "SELECT receipt.lock_statement_feed()",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()
        conn.execute("SET ROLE receipt_index_dev_read")
    conn.execute("RESET ROLE")
    conn.commit()
