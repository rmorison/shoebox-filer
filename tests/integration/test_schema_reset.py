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


def _table_exists(conn: Conn, schema: str, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s",
        (schema, table),
    ).fetchone()
    return row is not None


def _outside_objects(conn: Conn) -> dict[str, int | None]:
    """OIDs of what must survive. The bootstrap recreates the function and the
    extension when they are missing, so only an unchanged OID shows that the
    schema drop didn't take them with it."""
    oids: dict[str, int | None] = {}
    for name, query in (
        ("set_updated_at", "SELECT 'public.set_updated_at()'::regprocedure::oid"),
        ("pg_trgm", "SELECT oid FROM pg_extension WHERE extname = 'pg_trgm'"),
    ):
        row = conn.execute(query).fetchone()
        oids[name] = None if row is None else row["oid"]
    for table in _VERSION_TABLES:
        row = conn.execute(
            "SELECT to_regclass(%s)::oid", (f"public.{table}",)
        ).fetchone()
        oids[table] = None if row is None else row["to_regclass"]
    return oids


def test_bootstrap_drops_a_table_it_does_not_know(pg_conn: Conn) -> None:
    # Creates the version tables in public, so there is something to survive.
    apply_migration_grants(pg_conn)
    before = _outside_objects(pg_conn)
    assert all(before.values()), before
    pg_conn.execute(
        "CREATE TABLE receipt.stray (id int,"
        " receipt_id uuid REFERENCES receipt.receipts (id))"
    )
    pg_conn.commit()
    try:
        _run_migrations(pg_conn)

        assert not _table_exists(pg_conn, "receipt", "stray")
        assert _table_exists(pg_conn, "receipt", "receipts")
        assert _outside_objects(pg_conn) == before
    finally:
        # Leave the shared database clean even when the bootstrap didn't.
        pg_conn.rollback()
        pg_conn.execute("DROP TABLE IF EXISTS receipt.stray")
        pg_conn.commit()
