"""The bootstrap clears the whole receipt schema, not only the tables it knows.

Every worker shares one devtest database. A branch whose migration adds a
table leaves that table behind when its run ends; the next run, from a
branch without the migration, must not inherit it. The leftover here
references receipt.receipts, as a new migration's table usually would, so
dropping the schema must cascade through it, and nothing outside the
schema (the public trigger function, pg_trgm, the version tables) may go
with it.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from .conftest import _VERSION_TABLES, _run_migrations, apply_migration_grants

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]


def _exists(conn: Conn, query: str, *params: str) -> bool:
    row = conn.execute(query, params).fetchone()
    return row is not None


def test_bootstrap_drops_a_table_it_does_not_know(pg_conn: Conn) -> None:
    # The version tables live in public; make sure they are there to survive.
    apply_migration_grants(pg_conn)
    pg_conn.execute(
        "CREATE TABLE receipt.stray (id int,"
        " receipt_id uuid REFERENCES receipt.receipts (id))"
    )
    pg_conn.commit()
    try:
        _run_migrations(pg_conn)

        assert not _exists(
            pg_conn,
            "SELECT 1 FROM pg_tables WHERE schemaname = 'receipt'"
            " AND tablename = 'stray'",
        )
        assert _exists(
            pg_conn,
            "SELECT 1 FROM pg_tables WHERE schemaname = 'receipt'"
            " AND tablename = 'receipts'",
        )
        assert _exists(
            pg_conn,
            "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'public' AND p.proname = 'set_updated_at'",
        )
        assert _exists(pg_conn, "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        for table in _VERSION_TABLES:
            assert _exists(
                pg_conn,
                "SELECT 1 FROM pg_tables WHERE schemaname = 'public'"
                " AND tablename = %s",
                table,
            )
    finally:
        # Leave the shared database clean even when the bootstrap didn't.
        pg_conn.rollback()
        pg_conn.execute("DROP TABLE IF EXISTS receipt.stray")
        pg_conn.commit()
