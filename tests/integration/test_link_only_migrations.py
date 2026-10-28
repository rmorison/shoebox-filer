"""Migration 000013 (link-only receipts), up and down, with its grant.

Runs the real migration files against a scratch database, as the sync-state
migration tests do, so the shared ``receipt_index_dev`` schema is untouched.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from tests.integration import test_sync_state_migrations as _base
from tests.integration.test_sync_state_migrations import (
    _has_column,
    _migration,
    _run,
)

pytestmark = pytest.mark.integration

scratch_db = _base.scratch_db

_LINK = "https://receipts.example.com/r/abc123"


def _insert(conn: psycopg.Connection[Any], status: str, link: str) -> None:
    conn.execute(
        "INSERT INTO receipt.ingest_log (source_id, source_type, status, receipt_link)"
        " VALUES ('<link@example.com>', 'imap', %s, %s)",
        (status, link),
    )


class TestUpAndDown:
    def test_up_adds_the_column_and_down_drops_it(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        assert _has_column(conn, "ingest_log", "receipt_link")
        _insert(conn, "skipped", _LINK)
        conn.commit()

        _run(conn, _migration("receipt", "000013", "down"))
        assert not _has_column(conn, "ingest_log", "receipt_link")
        row = conn.execute("SELECT status FROM receipt.ingest_log").fetchone()
        assert row is not None
        assert row["status"] == "skipped"

        _run(conn, _migration("receipt", "000013", "up"))
        assert _has_column(conn, "ingest_log", "receipt_link")


class TestConstraint:
    @pytest.mark.parametrize("status", ["failed", "success", "queued", "started"])
    def test_a_link_only_goes_on_a_skipped_row(
        self, scratch_db: psycopg.Connection[dict[str, Any]], status: str
    ) -> None:
        with pytest.raises(psycopg.errors.CheckViolation, match="receipt_link"):
            _insert(scratch_db, status, _LINK)

    def test_a_link_over_2048_characters_is_refused(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        url = _LINK + "a" * (2049 - len(_LINK))
        with pytest.raises(psycopg.errors.CheckViolation, match="receipt_link"):
            _insert(scratch_db, "skipped", url)


class TestGrant:
    def test_the_write_role_can_insert_the_link(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        row = scratch_db.execute(
            "SELECT has_column_privilege('receipt_index_dev_write',"
            " 'receipt.ingest_log', 'receipt_link', 'INSERT') AS ok"
        ).fetchone()
        assert row is not None
        assert row["ok"] is True

    def test_the_read_role_can_read_it(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        row = scratch_db.execute(
            "SELECT has_column_privilege('receipt_index_dev_read',"
            " 'receipt.ingest_log', 'receipt_link', 'SELECT') AS ok"
        ).fetchone()
        assert row is not None
        assert row["ok"] is True
