"""Migration 000012 (retries), up and down, with its grants.

Runs the real migration files against a scratch database, as the sync-state
migration tests do, so the shared ``receipt_index_dev`` schema is untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.integration import test_sync_state_migrations as _base
from tests.integration.test_sync_state_migrations import (
    _has_column,
    _has_table,
    _migration,
    _run,
)

if TYPE_CHECKING:
    from uuid import UUID

pytestmark = pytest.mark.integration

# The scratch database every test here runs against (up migrations applied).
scratch_db = _base.scratch_db

_NEW_COLUMNS = (
    "attempt",
    "counts_attempt",
    "next_retry_at",
    "error_class",
    "stored_ref",
    "retry_request_id",
)


def _request(conn: psycopg.Connection[Any]) -> UUID:
    row = conn.execute(
        "INSERT INTO receipt.retry_requests (requested_by, filters, matched, requeued)"
        " VALUES ('operator', %s, 1, 0) RETURNING id",
        (Jsonb({"status": "failed"}),),
    ).fetchone()
    assert row is not None
    return row["id"]  # type: ignore[no-any-return]


class TestUpAndDown:
    def test_existing_failures_start_out_terminal(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        # A failed row written before 000012, then the migration applied.
        _run(conn, _migration("receipt", "000012", "down"))
        conn.execute(
            "INSERT INTO receipt.ingest_log (source_id, source_type, status) "
            "VALUES ('<old@example.com>', 'imap', 'failed')"
        )
        conn.commit()
        _run(conn, _migration("receipt", "000012", "up"))
        row = conn.execute(
            "SELECT attempt, counts_attempt, next_retry_at, error_class "
            "FROM receipt.ingest_log"
        ).fetchone()
        assert row == {
            "attempt": 1,
            "counts_attempt": True,
            "next_retry_at": None,
            "error_class": None,
        }

    def test_checks(self, scratch_db: psycopg.Connection[dict[str, Any]]) -> None:
        conn = scratch_db
        request = _request(conn)
        conn.execute(
            "INSERT INTO receipt.ingest_log (source_id, source_type, status, "
            "attempt, counts_attempt, retry_request_id) "
            "VALUES ('a', 'imap', 'queued', 0, false, %s)",
            (request,),
        )
        conn.commit()
        for bad in (
            "INSERT INTO receipt.ingest_log (source_id, source_type, status) "
            "VALUES ('b', 'imap', 'requeued')",
            "INSERT INTO receipt.ingest_log (source_id, source_type, status, "
            "error_class) VALUES ('b', 'imap', 'failed', 'network')",
            "INSERT INTO receipt.ingest_log (source_id, source_type, status, "
            "attempt) VALUES ('b', 'imap', 'failed', -1)",
            "UPDATE receipt.retry_requests SET requeued = 5",
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(bad)
            conn.rollback()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO receipt.ingest_log (source_id, source_type, status, "
                "retry_request_id) VALUES "
                "('b', 'imap', 'queued', '019572a0-0000-7000-8000-000000000000')"
            )
        conn.rollback()

    def test_down_with_queued_rows_then_up_again(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        request = _request(conn)
        conn.execute(
            "INSERT INTO receipt.ingest_log (source_id, source_type, status) "
            "VALUES ('<a@example.com>', 'imap', 'failed')"
        )
        conn.execute(
            "INSERT INTO receipt.ingest_log (source_id, source_type, status, "
            "retry_request_id) VALUES ('<a@example.com>', 'imap', 'queued', %s)",
            (request,),
        )
        conn.commit()

        _run(conn, _migration("receipt", "000012", "down"))

        assert not _has_table(conn, "receipt.retry_requests")
        for column in _NEW_COLUMNS:
            assert not _has_column(conn, "ingest_log", column)
        rows = conn.execute("SELECT status FROM receipt.ingest_log").fetchall()
        assert rows == [{"status": "failed"}]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO receipt.ingest_log (source_id, source_type, status) "
                "VALUES ('x', 'imap', 'queued')"
            )
        conn.rollback()

        _run(conn, _migration("receipt", "000012", "up"))
        assert _has_table(conn, "receipt.retry_requests")
        for column in _NEW_COLUMNS:
            assert _has_column(conn, "ingest_log", column)


class TestGrants:
    def test_write_role_can_log_retries_and_requeue(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        from receipt_index.repository import (
            insert_ingest_log,
            insert_retry_request,
            set_retry_request_count,
        )

        conn = scratch_db
        conn.execute("SET ROLE receipt_index_dev_write")
        conn.commit()
        request = insert_retry_request(
            conn, requested_by="operator", filters={"status": "failed"}, matched=1
        )
        insert_ingest_log(
            conn,
            source_id="<a@example.com>",
            source_type="imap",
            status="failed",
            attempt=2,
            counts_attempt=False,
            error_class="transient",
            stored_ref="local:renditions/2026/10/x.pdf",
        )
        insert_ingest_log(
            conn,
            source_id="<a@example.com>",
            source_type="imap",
            status="queued",
            attempt=0,
            counts_attempt=False,
            retry_request_id=request,
        )
        set_retry_request_count(conn, request, 1)
        row = conn.execute(
            "SELECT requested_by, db_user, requeued FROM receipt.retry_requests"
        ).fetchone()
        assert row == {
            "requested_by": "operator",
            "db_user": "receipt_index_dev_write",
            "requeued": 1,
        }
        conn.execute("RESET ROLE")
        conn.commit()

    def test_write_role_cannot_forge_the_audit_columns(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        conn.execute("SET ROLE receipt_index_dev_write")
        for bad in (
            "INSERT INTO receipt.retry_requests (requested_by, filters, matched, "
            "requeued, db_user) VALUES ('x', '{}', 0, 0, 'someone')",
            "INSERT INTO receipt.retry_requests (requested_by, filters, matched, "
            "requeued, requested_at) VALUES ('x', '{}', 0, 0, '2000-01-01')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(bad)
            conn.rollback()
            conn.execute("SET ROLE receipt_index_dev_write")
        _request(conn)
        for bad in (
            "UPDATE receipt.retry_requests SET filters = '{}'",
            "DELETE FROM receipt.retry_requests",
            "UPDATE receipt.ingest_log SET next_retry_at = now()",
            "DELETE FROM receipt.ingest_log",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(bad)
            conn.rollback()
            conn.execute("SET ROLE receipt_index_dev_write")
        conn.execute("RESET ROLE")
        conn.rollback()

    def test_read_role_reads_requests_only(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        conn.execute("SET ROLE receipt_index_dev_read")
        conn.execute("SELECT * FROM receipt.retry_requests")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _request(conn)
        conn.rollback()
