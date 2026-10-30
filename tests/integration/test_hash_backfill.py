"""``store backfill-hashes``: recording renditions stored before the schema.

Each existing receipt gets one ``stored_files`` row, one ``local`` rendition
location and its ``rendition_file_id``, and a second run changes nothing.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import psycopg
import pytest

from receipt_index.hash_backfill import backfill_rendition_hashes
from receipt_index.repository import insert_receipt
from receipt_index.store import LocalEvidenceStore

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID


pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]


def _receipt(
    conn: Conn, root: Path, source_id: str, pdf_path: str, data: bytes | None
) -> UUID:
    """A receipt as ingest stored it before the schema: a file, no records."""
    if data is not None:
        path = root / pdf_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    receipt = insert_receipt(
        conn,
        source_id=source_id,
        source_type="imap",
        source_name="personal-email",
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal("12.00"),
        currency="USD",
        receipt_date=date(2026, 1, 15),
        description=None,
        confidence=0.9,
        pdf_path=pdf_path,
        email_subject="Parking",
        email_sender="user@example.com",
        email_date=None,
    )
    return receipt.id


def _state(conn: Conn) -> dict[str, Any]:
    files = conn.execute(
        "SELECT id, sha256, byte_size, media_type, updated_at"
        " FROM receipt.stored_files ORDER BY id"
    ).fetchall()
    locations = conn.execute(
        "SELECT file_id, backend, locator, purpose, receipt_id, owner,"
        " display_path, updated_at FROM receipt.file_locations ORDER BY locator"
    ).fetchall()
    receipts = conn.execute(
        "SELECT id, rendition_file_id, updated_at FROM receipt.receipts ORDER BY id"
    ).fetchall()
    conn.rollback()
    return {"files": files, "locations": locations, "receipts": receipts}


def test_each_receipt_gets_its_file_location_and_id(
    pg_conn: Conn, tmp_path: Path
) -> None:
    store = LocalEvidenceStore(tmp_path)
    first = _receipt(pg_conn, tmp_path, "<a@example.com>", "2026/01/a.pdf", b"%PDF-a")
    second = _receipt(
        pg_conn, tmp_path, "<b@example.com>", "renditions/2026/01/b.pdf", b"%PDF-b"
    )

    report = backfill_rendition_hashes(pg_conn, store)

    assert (report.hashed, report.shared, report.missing) == (2, 0, [])
    state = _state(pg_conn)
    assert len(state["files"]) == 2
    by_hash = {bytes(f["sha256"]): f for f in state["files"]}
    a_file = by_hash[hashlib.sha256(b"%PDF-a").digest()]
    assert (a_file["byte_size"], a_file["media_type"]) == (6, "application/pdf")
    locations = {loc["receipt_id"]: loc for loc in state["locations"]}
    assert set(locations) == {first, second}
    a_loc = locations[first]
    assert (a_loc["backend"], a_loc["locator"], a_loc["purpose"]) == (
        "local",
        "2026/01/a.pdf",
        "rendition",
    )
    assert (a_loc["owner"], a_loc["display_path"]) == ("store", "2026/01/a.pdf")
    assert a_loc["file_id"] == a_file["id"]
    rendition = {r["id"]: r["rendition_file_id"] for r in state["receipts"]}
    assert rendition == {first: a_file["id"], second: locations[second]["file_id"]}


def test_a_second_run_changes_nothing(pg_conn: Conn, tmp_path: Path) -> None:
    store = LocalEvidenceStore(tmp_path)
    _receipt(pg_conn, tmp_path, "<a@example.com>", "2026/01/a.pdf", b"%PDF-a")
    backfill_rendition_hashes(pg_conn, store)
    before = _state(pg_conn)

    report = backfill_rendition_hashes(pg_conn, store)

    assert (report.hashed, report.shared, report.missing) == (0, 0, [])
    assert _state(pg_conn) == before


def test_identical_bytes_share_a_file_with_a_location_each(
    pg_conn: Conn, tmp_path: Path
) -> None:
    store = LocalEvidenceStore(tmp_path)
    first = _receipt(pg_conn, tmp_path, "<a@example.com>", "2026/01/a.pdf", b"%PDF-x")
    second = _receipt(pg_conn, tmp_path, "<b@example.com>", "2026/01/b.pdf", b"%PDF-x")

    report = backfill_rendition_hashes(pg_conn, store)

    assert (report.hashed, report.shared) == (2, 1)
    state = _state(pg_conn)
    (stored,) = state["files"]
    assert {loc["receipt_id"] for loc in state["locations"]} == {first, second}
    assert {loc["file_id"] for loc in state["locations"]} == {stored["id"]}


def test_a_missing_file_is_reported_and_picked_up_later(
    pg_conn: Conn, tmp_path: Path
) -> None:
    store = LocalEvidenceStore(tmp_path)
    receipt_id = _receipt(pg_conn, tmp_path, "<a@example.com>", "2026/01/a.pdf", None)

    report = backfill_rendition_hashes(pg_conn, store)

    assert report.hashed == 0
    assert [rid for rid, _ in report.missing] == [receipt_id]
    assert not report.ok
    assert _state(pg_conn)["files"] == []

    (tmp_path / "2026" / "01").mkdir(parents=True)
    (tmp_path / "2026" / "01" / "a.pdf").write_bytes(b"%PDF-a")
    report = backfill_rendition_hashes(pg_conn, store)
    assert (report.hashed, report.missing) == (1, [])


def test_dry_run_writes_nothing(pg_conn: Conn, tmp_path: Path) -> None:
    store = LocalEvidenceStore(tmp_path)
    _receipt(pg_conn, tmp_path, "<a@example.com>", "2026/01/a.pdf", b"%PDF-a")
    before = _state(pg_conn)

    report = backfill_rendition_hashes(pg_conn, store, dry_run=True)

    assert report.hashed == 1
    assert _state(pg_conn) == before
