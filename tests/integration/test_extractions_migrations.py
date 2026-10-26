"""Migration 000011: extraction history and its append-only guard.

Runs the real migration files, grants included, against a scratch database
that the fixture creates and drops, so it never touches the shared
``receipt_index_dev`` schema. The guard must hold for the role that owns the
table: in production the application connects as the owner.
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


def _migration(version: str, direction: str) -> Path:
    (path,) = (_MIGRATIONS / "receipt").glob(f"{version}_*.{direction}.sql")
    return path


def _run(conn: psycopg.Connection[Any], path: Path) -> None:
    conn.execute(path.read_text())
    conn.commit()


@pytest.fixture
def scratch() -> Iterator[tuple[psycopg.Connection[dict[str, Any]], str]]:
    """A fresh database with every up migration, and a role that owns nothing.

    Yields the superuser connection and the name of a scratch login role
    that the tests make the owner of ``receipt.extractions``.
    """
    try:
        admin = psycopg.connect(_ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip(f"Postgres not available at localhost:{_PORT}")
    suffix = uuid.uuid4().hex[:8]
    name = f"receipt_index_test_extr_{suffix}"
    owner = f"receipt_index_test_owner_{suffix}"
    for role in _ROLES:  # CI's Postgres has no application roles
        admin.execute(
            sql.SQL(
                "DO $$ BEGIN IF NOT EXISTS "
                "(SELECT FROM pg_roles WHERE rolname = {lit}) THEN "
                "CREATE ROLE {ident} NOLOGIN; END IF; END $$"
            ).format(lit=sql.Literal(role), ident=sql.Identifier(role))
        )
    admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(owner)))
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = _ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"
    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        for schema in ("public", "receipt"):
            for path in _files(schema, "up"):
                _run(conn, path)
        yield conn, owner
    finally:
        conn.close()
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(owner)))
        admin.close()


def _receipt(conn: psycopg.Connection[Any], source_id: str) -> Any:
    row = conn.execute(
        "INSERT INTO receipt.receipts (source_id, source_name, vendor, amount,"
        " receipt_date, confidence, pdf_path) VALUES (%s, 'personal-email',"
        " 'Acme Parking (U.S.), LLC', 12.00, '2026-01-15', 0.9, '2026/01/x.pdf')"
        " RETURNING id",
        (source_id,),
    ).fetchone()
    assert row is not None
    return row["id"]


def _extraction(conn: psycopg.Connection[Any], receipt_id: Any) -> Any:
    row = conn.execute(
        "INSERT INTO receipt.extractions (receipt_id, origin, input_kind,"
        " prompt_version, llm_model, output) VALUES"
        " (%s, 'ingest', 'source', 2, 'test-model', '{}') RETURNING id",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    return row["id"]


def _count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT count(*) AS n FROM receipt.extractions").fetchone()
    assert row is not None
    return int(row["n"])


_CHANGES = {
    "update": "UPDATE receipt.extractions SET llm_model = 'other'",
    "delete": "DELETE FROM receipt.extractions",
    "truncate": "TRUNCATE receipt.extractions",
}


class TestUpAndDown:
    def test_up_down_up_over_existing_rows(
        self, scratch: tuple[psycopg.Connection[dict[str, Any]], str]
    ) -> None:
        conn, _ = scratch
        receipt_id = _receipt(conn, "<keep@example.com>")
        _extraction(conn, receipt_id)
        conn.commit()

        _run(conn, _migration("000011", "down"))
        row = conn.execute(
            "SELECT to_regclass('receipt.extractions') AS t,"
            " to_regprocedure('receipt.forbid_history_change()') AS f"
        ).fetchone()
        assert row == {"t": None, "f": None}
        kept = conn.execute(
            "SELECT count(*) AS n FROM receipt.receipts WHERE id = %s", (receipt_id,)
        ).fetchone()
        assert kept == {"n": 1}

        _run(conn, _migration("000011", "up"))
        row = conn.execute(
            "SELECT manual_fields FROM receipt.receipts WHERE id = %s", (receipt_id,)
        ).fetchone()
        assert row == {"manual_fields": []}
        _extraction(conn, receipt_id)
        conn.commit()


class TestAppendOnly:
    @pytest.mark.parametrize("change", sorted(_CHANGES))
    def test_refused_for_the_superuser(
        self, scratch: tuple[psycopg.Connection[dict[str, Any]], str], change: str
    ) -> None:
        conn, _ = scratch
        _extraction(conn, _receipt(conn, "<a@example.com>"))
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(_CHANGES[change])
        conn.rollback()
        assert _count(conn) == 1

    @pytest.mark.parametrize("change", sorted(_CHANGES))
    def test_refused_for_the_owning_role(
        self, scratch: tuple[psycopg.Connection[dict[str, Any]], str], change: str
    ) -> None:
        conn, owner = scratch
        _extraction(conn, _receipt(conn, "<b@example.com>"))
        ident = sql.Identifier(owner)
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA receipt TO {}").format(ident))
        # As in production, one role owns every table, receipts included
        # (the guard reads receipts to tell a cascade from a direct delete).
        for table in ("receipts", "extractions"):
            conn.execute(
                sql.SQL("ALTER TABLE receipt.{} OWNER TO {}").format(
                    sql.Identifier(table), ident
                )
            )
        conn.commit()

        conn.execute(sql.SQL("SET ROLE {}").format(ident))
        owns = conn.execute(
            "SELECT tableowner = current_user AS owns FROM pg_tables"
            " WHERE schemaname = 'receipt' AND tablename = 'extractions'"
        ).fetchone()
        assert owns == {"owns": True}
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(_CHANGES[change])
        conn.rollback()
        conn.execute("RESET ROLE")
        assert _count(conn) == 1

    def test_deleting_the_receipt_cascades(
        self, scratch: tuple[psycopg.Connection[dict[str, Any]], str]
    ) -> None:
        conn, _ = scratch
        gone = _receipt(conn, "<gone@example.com>")
        kept = _receipt(conn, "<kept@example.com>")
        _extraction(conn, gone)
        _extraction(conn, kept)
        conn.commit()

        conn.execute("DELETE FROM receipt.receipts WHERE id = %s", (gone,))
        conn.commit()
        rows = conn.execute("SELECT receipt_id FROM receipt.extractions").fetchall()
        assert rows == [{"receipt_id": kept}]

    def test_insert_is_allowed(
        self, scratch: tuple[psycopg.Connection[dict[str, Any]], str]
    ) -> None:
        conn, _ = scratch
        receipt_id = _receipt(conn, "<c@example.com>")
        _extraction(conn, receipt_id)
        _extraction(conn, receipt_id)
        conn.commit()
        assert _count(conn) == 2
