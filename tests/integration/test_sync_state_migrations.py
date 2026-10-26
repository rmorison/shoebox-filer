"""Migrations 000009 (sync_state) and 000010 (ingest_log 'started').

These tests run the real migration files, up and down, against a scratch
database they create and drop, so they never touch the shared
``receipt_index_dev`` schema that the other integration tests rebuild.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"
_PORT = os.environ.get("POSTGRES_HOST_PORT", "15432")
_ADMIN_DSN = (
    "postgresql://main:localpass@localhost:"  # pragma: allowlist secret
    f"{_PORT}/receipt_index_dev"
)
_ROLES = ("receipt_index_dev_all", "receipt_index_dev_write", "receipt_index_dev_read")


def _files(schema: str, direction: str) -> list[Path]:
    files = sorted((_MIGRATIONS / schema).glob(f"*.{direction}.sql"))
    return files if direction == "up" else list(reversed(files))


def _run(conn: psycopg.Connection[Any], path: Path) -> None:
    conn.execute(path.read_text())  # no parameters: several statements are fine
    conn.commit()


def _migration(schema: str, version: str, direction: str) -> Path:
    (path,) = (_MIGRATIONS / schema).glob(f"{version}_*.{direction}.sql")
    return path


@pytest.fixture
def scratch_db() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """A fresh database with every up migration applied, dropped afterwards."""
    try:
        admin = psycopg.connect(_ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip(f"Postgres not available at localhost:{_PORT}")
    name = f"receipt_index_test_sync_{uuid.uuid4().hex[:8]}"
    for role in _ROLES:  # CI's Postgres has no application roles
        admin.execute(
            sql.SQL(
                "DO $$ BEGIN IF NOT EXISTS "
                "(SELECT FROM pg_roles WHERE rolname = {lit}) THEN "
                "CREATE ROLE {ident} NOLOGIN; END IF; END $$"
            ).format(lit=sql.Literal(role), ident=sql.Identifier(role))
        )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = _ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"
    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        for schema in ("public", "receipt"):
            for path in _files(schema, "up"):
                _run(conn, path)
        yield conn
    finally:
        conn.close()
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )
        admin.close()


def _has_table(conn: psycopg.Connection[Any], table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()
    return row is not None and row["t"] is not None


def _has_column(conn: psycopg.Connection[Any], table: str, column: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'receipt' AND table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    return row is not None


def _insert_log(conn: psycopg.Connection[Any], source_id: str, status: str) -> None:
    conn.execute(
        "INSERT INTO receipt.ingest_log (source_id, source_type, status, source_name)"
        " VALUES (%s, 'imap', %s, 'personal-email')",
        (source_id, status),
    )


class TestUpAndDown:
    def test_up_creates_sync_state_and_allows_started(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        assert _has_table(conn, "receipt.sync_state")
        assert _has_column(conn, "ingest_log", "source_name")
        _insert_log(conn, "<a@example.com>", "started")
        conn.commit()

    def test_sync_state_cursor_checks(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        conn.execute(
            "INSERT INTO receipt.sync_state "
            "(source_name, source_type, folder, imap_uidvalidity, imap_last_uid) "
            "VALUES ('personal-email', 'imap', 'INBOX', 7, 42)"
        )
        conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO receipt.sync_state "
                "(source_name, source_type, folder, drive_page_token, imap_last_uid) "
                "VALUES ('scans', 'gdrive', 'folder-1', 'tok', 3)"
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO receipt.sync_state (source_name, source_type, folder) "
                "VALUES ('personal-email', 'imap', 'INBOX')"
            )
        conn.rollback()

    def test_updated_at_trigger(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        conn.execute(
            "INSERT INTO receipt.sync_state (source_name, source_type, folder, "
            "updated_at) VALUES ('p', 'imap', 'INBOX', '2000-01-01')"
        )
        conn.execute("UPDATE receipt.sync_state SET imap_last_uid = 1")
        row = conn.execute("SELECT updated_at FROM receipt.sync_state").fetchone()
        assert row is not None
        assert row["updated_at"].year > 2000
        conn.rollback()

    def test_down_with_started_rows_then_up_again(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        _insert_log(conn, "<started@example.com>", "started")
        _insert_log(conn, "<done@example.com>", "success")
        conn.commit()

        _run(conn, _migration("receipt", "000010", "down"))
        _run(conn, _migration("receipt", "000009", "down"))

        assert not _has_table(conn, "receipt.sync_state")
        assert not _has_column(conn, "ingest_log", "source_name")
        rows = conn.execute(
            "SELECT source_id, status FROM receipt.ingest_log"
        ).fetchall()
        assert rows == [{"source_id": "<done@example.com>", "status": "success"}]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO receipt.ingest_log (source_id, source_type, status) "
                "VALUES ('x', 'imap', 'started')"
            )
        conn.rollback()

        _run(conn, _migration("receipt", "000009", "up"))
        _run(conn, _migration("receipt", "000010", "up"))
        assert _has_table(conn, "receipt.sync_state")
        assert _has_column(conn, "ingest_log", "source_name")


class TestGrants:
    def test_write_role_can_advance_cursors_and_log_started(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        conn.execute("SET ROLE receipt_index_dev_write")
        conn.execute(
            "INSERT INTO receipt.sync_state (source_name, source_type, folder) "
            "VALUES ('personal-email', 'imap', 'INBOX')"
        )
        conn.execute(
            "UPDATE receipt.sync_state SET imap_uidvalidity = 1, imap_last_uid = 9, "
            "last_poll_at = now() WHERE source_name = 'personal-email'"
        )
        _insert_log(conn, "<w@example.com>", "started")
        row = conn.execute("SELECT count(*) AS n FROM receipt.sync_state").fetchone()
        assert row is not None
        assert row["n"] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM receipt.sync_state")
        conn.rollback()

    def test_write_role_cannot_set_database_owned_columns(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        conn.execute("SET ROLE receipt_index_dev_write")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO receipt.sync_state "
                "(source_name, source_type, folder, created_at) "
                "VALUES ('x', 'imap', 'INBOX', '2000-01-01')"
            )
        conn.rollback()
        conn.execute("SET ROLE receipt_index_dev_write")
        conn.execute(
            "INSERT INTO receipt.sync_state (source_name, source_type, folder) "
            "VALUES ('x', 'imap', 'INBOX')"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE receipt.sync_state SET folder = 'Other'")
        conn.rollback()

    def test_write_role_can_run_the_repository_upserts(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        """The cursor and poll upserts need exactly the granted columns."""
        from receipt_index.repository import (
            record_poll,
            save_drive_cursor,
            save_imap_cursor,
        )

        conn = scratch_db
        conn.execute("SET ROLE receipt_index_dev_write")
        conn.commit()
        record_poll(conn, source_name="m", source_type="imap", folder="I", error=None)
        record_poll(conn, source_name="m", source_type="imap", folder="I", error="x")
        save_imap_cursor(conn, source_name="m", folder="I", uidvalidity=1, last_uid=2)
        save_imap_cursor(
            conn, source_name="m", folder="I", uidvalidity=3, last_uid=0, reset=True
        )
        save_drive_cursor(
            conn, source_name="d", folder="F", page_token="t", account_id="a"
        )
        save_drive_cursor(
            conn, source_name="d", folder="F", page_token="u", account_id="a"
        )
        conn.execute("RESET ROLE")
        conn.commit()

    def test_read_role_cannot_write_sync_state(
        self, scratch_db: psycopg.Connection[dict[str, Any]]
    ) -> None:
        conn = scratch_db
        conn.execute("SET ROLE receipt_index_dev_read")
        conn.execute("SELECT * FROM receipt.sync_state")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO receipt.sync_state (source_name, source_type, folder) "
                "VALUES ('x', 'imap', 'INBOX')"
            )
        conn.rollback()
