"""Ingest writes provenance and rendition records, and is idempotent on provenance.

A new receipt gets one primary provenance row, its rendition's
``stored_files`` row, its own ``local`` location and ``rendition_file_id``,
all in the ingest's transaction. An item already in ``provenance`` is never
ingested again, whichever source it arrives from, and whether or not
``receipts`` still names it.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import psycopg
import pytest

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.pipeline import run_ingest
from receipt_index.repository import insert_provenance, insert_receipt
from receipt_index.store import LocalEvidenceStore, rendition_ref

from .conftest import seed_email
from .test_e2e_pipeline import _greenmail_imap_config, _make_mock_agent, _unique_user

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]


def _ingest(conn: Conn, user: str, store: LocalEvidenceStore, source: str) -> Any:
    config = _greenmail_imap_config(user).model_copy(update={"name": source})
    return run_ingest(
        conn=conn,
        adapter=ImapAdapter(config),
        store=store,
        source_name=source,
        source_type="imap",
        agent=_make_mock_agent(),
        llm_model="test-model",
    )


def _provenance(conn: Conn, source_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM receipt.provenance WHERE source_id = %s", (source_id,)
    ).fetchall()
    conn.rollback()
    return rows


def test_a_new_receipt_records_its_provenance_and_rendition(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user = _unique_user("prov")
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    seed_email(
        to_addr=user,
        from_addr="shop@example.com",
        subject="Your parking receipt",
        text_body="Total: $42.99",
        message_id=message_id,
    )
    store = LocalEvidenceStore(tmp_path)

    result = _ingest(pg_conn, user, store, "greenmail-test")

    (receipt,) = result.receipts
    (prov,) = _provenance(pg_conn, receipt.source_id)
    assert prov["source_id"] == message_id
    assert prov["receipt_id"] == receipt.id
    assert prov["home_receipt_id"] == receipt.id
    assert prov["is_primary"] is True
    assert prov["match_method"] == "first"
    assert prov["original_status"] == "pending"
    assert (prov["source_type"], prov["source_name"]) == ("imap", "greenmail-test")
    assert prov["email_subject"] == "Your parking receipt"
    assert prov["received_at"] is not None

    with store.open(rendition_ref(receipt.pdf_path)) as fh:
        digest = hashlib.sha256(fh.read()).digest()
    row = pg_conn.execute(
        "SELECT f.id, f.sha256, l.backend, l.locator, l.receipt_id,"
        " r.rendition_file_id"
        " FROM receipt.receipts r"
        " JOIN receipt.file_locations l ON l.receipt_id = r.id"
        " JOIN receipt.stored_files f ON f.id = l.file_id"
        " WHERE r.id = %s",
        (receipt.id,),
    ).fetchone()
    pg_conn.rollback()
    assert row is not None
    assert bytes(row["sha256"]) == digest
    assert (row["backend"], row["locator"]) == ("local", receipt.pdf_path)
    assert row["rendition_file_id"] == row["id"]


def test_the_same_message_in_a_second_source_is_skipped(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    first, second = _unique_user("prov-a"), _unique_user("prov-b")
    for user in (first, second):
        seed_email(
            to_addr=user,
            subject="Your parking receipt",
            text_body="Total: $42.99",
            message_id=message_id,
        )
    store = LocalEvidenceStore(tmp_path)

    assert _ingest(pg_conn, first, store, "mailbox-a").processed == 1
    again = _ingest(pg_conn, second, store, "mailbox-b")

    assert again.processed == 0
    assert len(_provenance(pg_conn, message_id)) == 1


def test_an_item_known_only_to_provenance_is_skipped(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    """A receipt that no longer names its source (as after cutover) still counts."""
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    receipt = insert_receipt(
        pg_conn,
        source_id=f"legacy-{uuid.uuid4().hex}",
        source_type="imap",
        source_name="mailbox-a",
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal("12.00"),
        currency="USD",
        receipt_date=date(2026, 1, 15),
        description=None,
        confidence=0.9,
        pdf_path="2026/01/legacy.pdf",
        email_subject=None,
        email_sender=None,
        email_date=None,
        commit=False,
    )
    pg_conn.execute(
        "UPDATE receipt.receipts SET source_id = NULL WHERE id = %s", (receipt.id,)
    )
    insert_provenance(
        pg_conn,
        receipt_id=receipt.id,
        source_type="imap",
        source_name="mailbox-a",
        source_id=message_id,
        received_at=receipt.created_at,
        email_subject=None,
        email_sender=None,
        email_date=None,
        file_name=None,
        match_method="migrated",
    )
    pg_conn.commit()
    user = _unique_user("prov-known")
    seed_email(
        to_addr=user,
        subject="Your parking receipt",
        text_body="Total: $42.99",
        message_id=message_id,
    )

    result = _ingest(pg_conn, user, LocalEvidenceStore(tmp_path), "mailbox-b")

    # Skipped before it was fetched: not tried and refused by the unique
    # constraint on provenance.source_id, which would leave a failed row.
    assert (result.processed, result.failed) == (0, 0)
    count = pg_conn.execute("SELECT count(*) AS n FROM receipt.receipts").fetchone()
    logged = pg_conn.execute(
        "SELECT count(*) AS n FROM receipt.ingest_log WHERE source_id = %s",
        (message_id,),
    ).fetchone()
    pg_conn.rollback()
    assert count == {"n": 1}
    assert logged == {"n": 0}
