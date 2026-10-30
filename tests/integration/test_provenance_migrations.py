"""Migrations 000017 and 000018: the evidence-store tables and provenance backfill.

Runs the real migration files, grants included, in a scratch database (the
``scratch`` fixture from the 000011 tests, which creates and drops it), so it
never touches the shared ``receipt_index_dev`` schema.
"""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
import pytest
from psycopg import errors

from .test_extractions_migrations import (  # noqa: F401 (scratch is a fixture)
    _migration,
    _receipt,
    _run,
    scratch,
)

pytestmark = pytest.mark.integration

Scratch = tuple[psycopg.Connection[dict[str, Any]], str]
Conn = psycopg.Connection[dict[str, Any]]

_TABLES = ("stored_files", "file_locations", "provenance", "provenance_files")
_LEGACY = ("pdf_path", "source_id", "source_name", "source_type")


def _tables(conn: Conn) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'receipt' AND table_name = ANY(%s)",
        (list(_TABLES),),
    ).fetchall()
    return {r["table_name"] for r in rows}


def _nullable(conn: Conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns"
        " WHERE table_schema = 'receipt' AND table_name = 'receipts'"
        " AND column_name = ANY(%s)",
        (list(_LEGACY),),
    ).fetchall()
    return {r["column_name"]: r["is_nullable"] for r in rows}


def _provenance(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM receipt.provenance ORDER BY source_id"
    ).fetchall()


def _seed(conn: Conn) -> tuple[Any, Any]:
    """An email receipt with a date, and a Drive receipt without one."""
    email = _receipt(conn, "<a@example.com>")
    conn.execute(
        "UPDATE receipt.receipts SET email_date = '2026-01-15 10:00+00',"
        " email_subject = 'Parking', email_sender = 'user@example.com'"
        " WHERE id = %s",
        (email,),
    )
    drive = _receipt(conn, "drive-file-1")
    conn.execute(
        "UPDATE receipt.receipts SET source_type = 'gdrive', source_name = 'scans',"
        " file_name = 'scan.pdf' WHERE id = %s",
        (drive,),
    )
    conn.commit()
    return email, drive


def _backfill_again(conn: Conn) -> None:
    conn.execute("DELETE FROM receipt.provenance")
    conn.commit()
    _run(conn, _migration("000018", "up"))


class TestUpAndDown:
    def test_down_then_up_over_existing_rows(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        _seed(conn)
        _backfill_again(conn)
        assert len(_provenance(conn)) == 2

        _run(conn, _migration("000018", "down"))
        assert _provenance(conn) == []
        _run(conn, _migration("000017", "down"))
        assert _tables(conn) == set()
        assert _nullable(conn) == dict.fromkeys(_LEGACY, "NO")
        default = conn.execute(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_schema = 'receipt' AND table_name = 'receipts'"
            " AND column_name = 'source_type'"
        ).fetchone()
        assert default == {"column_default": "'imap'::text"}

        _run(conn, _migration("000017", "up"))
        _run(conn, _migration("000018", "up"))
        assert _tables(conn) == set(_TABLES)
        assert _nullable(conn) == dict.fromkeys(_LEGACY, "YES")
        assert len(_provenance(conn)) == 2
        count = conn.execute("SELECT count(*) AS n FROM receipt.receipts").fetchone()
        assert count == {"n": 2}


class TestProvenanceBackfill:
    def test_one_primary_row_per_receipt_copied_from_it(
        self,
        scratch: Scratch,  # noqa: F811
    ) -> None:
        conn, _ = scratch
        email, drive = _seed(conn)
        _backfill_again(conn)
        rows = {r["source_id"]: r for r in _provenance(conn)}
        assert set(rows) == {"<a@example.com>", "drive-file-1"}

        mail = rows["<a@example.com>"]
        assert mail["receipt_id"] == email
        assert mail["home_receipt_id"] == email
        assert mail["is_primary"] is True
        assert mail["match_method"] == "migrated"
        assert mail["original_status"] == "pending"
        assert (mail["source_type"], mail["source_name"]) == ("imap", "personal-email")
        assert mail["email_subject"] == "Parking"
        assert mail["email_sender"] == "user@example.com"
        assert mail["received_at"] == mail["email_date"]

        scan = rows["drive-file-1"]
        assert scan["receipt_id"] == drive
        assert (scan["source_type"], scan["file_name"]) == ("gdrive", "scan.pdf")
        created = conn.execute(
            "SELECT created_at FROM receipt.receipts WHERE id = %s", (drive,)
        ).fetchone()
        assert created is not None
        assert scan["received_at"] == created["created_at"]

    def test_a_second_run_adds_nothing(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        _seed(conn)
        _backfill_again(conn)
        before = _provenance(conn)
        _run(conn, _migration("000018", "up"))
        assert _provenance(conn) == before


class TestConstraints:
    """The named constraints refuse what the design rules out."""

    def _file(self, conn: Conn, data: bytes = b"%PDF-1") -> Any:
        row = conn.execute(
            "INSERT INTO receipt.stored_files (sha256, byte_size, media_type)"
            " VALUES (%s, %s, 'application/pdf') RETURNING id",
            (hashlib.sha256(data).digest(), len(data)),
        ).fetchone()
        assert row is not None
        return row["id"]

    def _location(
        self, conn: Conn, file_id: Any, locator: str, receipt_id: Any
    ) -> None:
        conn.execute(
            "INSERT INTO receipt.file_locations (file_id, backend, locator, purpose,"
            " receipt_id, display_path) VALUES (%s, 'local', %s, 'rendition', %s, %s)",
            (file_id, locator, receipt_id, locator),
        )

    def _refused(self, conn: Conn, constraint: str, statement: str, *args: Any) -> None:
        with pytest.raises(errors.IntegrityError) as caught:
            conn.execute(statement, args)  # type: ignore[arg-type]
        conn.rollback()
        assert caught.value.diag.constraint_name == constraint

    def test_sha256_must_be_32_bytes(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        self._refused(
            conn,
            "ck_stored_files_sha256_present",
            "INSERT INTO receipt.stored_files (sha256, media_type)"
            " VALUES (%s, 'application/pdf')",
            b"short",
        )

    def test_same_bytes_are_one_row(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        self._file(conn)
        self._refused(
            conn,
            "uq_stored_files_sha256",
            "INSERT INTO receipt.stored_files (sha256, media_type)"
            " VALUES (%s, 'application/pdf')",
            hashlib.sha256(b"%PDF-1").digest(),
        )

    def test_rendition_location_names_its_receipt(
        self,
        scratch: Scratch,  # noqa: F811
    ) -> None:
        conn, _ = scratch
        file_id = self._file(conn)
        self._refused(
            conn,
            "ck_file_locations_rendition_receipt",
            "INSERT INTO receipt.file_locations (file_id, backend, locator, purpose,"
            " display_path) VALUES (%s, 'local', 'a.pdf', 'rendition', 'a.pdf')",
            file_id,
        )

    def test_one_rendition_location_per_receipt_and_backend(
        self,
        scratch: Scratch,  # noqa: F811
    ) -> None:
        conn, _ = scratch
        receipt_id = _receipt(conn, "<r@example.com>")
        file_id = self._file(conn)
        self._location(conn, file_id, "2026/01/a.pdf", receipt_id)
        conn.commit()
        self._refused(
            conn,
            "uq_file_locations_rendition_per_receipt",
            "INSERT INTO receipt.file_locations (file_id, backend, locator, purpose,"
            " receipt_id, display_path)"
            " VALUES (%s, 'local', 'b.pdf', 'rendition', %s, 'b.pdf')",
            file_id,
            receipt_id,
        )

    def test_one_primary_per_receipt_and_unique_source_id(
        self,
        scratch: Scratch,  # noqa: F811
    ) -> None:
        conn, _ = scratch
        receipt_id = _receipt(conn, "<p@example.com>")
        conn.commit()
        _backfill_again(conn)
        insert = (
            "INSERT INTO receipt.provenance (receipt_id, is_primary, source_type,"
            " source_name, source_id, received_at, match_method)"
            " VALUES (%s, %s, 'imap', 'mail', %s, now(), 'first')"
        )
        self._refused(
            conn,
            "uq_provenance_primary_per_receipt",
            insert,
            receipt_id,
            True,
            "<other@example.com>",
        )
        self._refused(
            conn,
            "uq_provenance_source_id",
            insert,
            receipt_id,
            False,
            "<p@example.com>",
        )

    def test_extraction_input_file_must_be_stored(
        self,
        scratch: Scratch,  # noqa: F811
    ) -> None:
        conn, _ = scratch
        receipt_id = _receipt(conn, "<x@example.com>")
        conn.commit()
        self._refused(
            conn,
            "fk_extractions_input_file",
            "INSERT INTO receipt.extractions (receipt_id, origin, input_kind,"
            " input_file_id, prompt_version, llm_model, output) VALUES"
            " (%s, 'ingest', 'source', gen_random_uuid(), 2, 'test-model', '{}')",
            receipt_id,
        )
