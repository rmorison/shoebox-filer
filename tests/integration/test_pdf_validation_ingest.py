"""PDF validation and repair at ingest (evidence-store design, §9).

Every PDF entering the store is checked in strict mode. An ``ok`` PDF
attachment is its own rendition. A ``repaired`` one is stored as sent and
its repair becomes the rendition, a new ``stored_files`` row pointing at it.
An ``unparseable`` one is stored as sent and the email body is rendered
instead; with no body, the item fails for the retry path. Every fixture is
built in the test.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import pytest

from receipt_index.models import RawReceipt
from receipt_index.pdf_validate import check_pdf
from receipt_index.pipeline import process_item
from receipt_index.store import LocalEvidenceStore
from tests.pdfs import garbage_pdf, make_pdf, truncated_pdf

from .conftest import seed_email
from .test_e2e_pipeline import _unique_user
from .test_originals_ingest import _agent, _ingest, _linked_files, _provenance

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]


def _seed(user: str, pdf: bytes, *, text_body: str | None = "See attached.") -> str:
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    seed_email(
        to_addr=user,
        subject="Your parking receipt",
        text_body=text_body,
        message_id=message_id,
        attachments=[("receipt.pdf", "application/pdf", pdf)],
    )
    return message_id


def _rendition(conn: Conn, receipt_id: Any) -> dict[str, Any]:
    row = conn.execute(
        "SELECT f.id, f.sha256, f.pdf_status, f.repaired_from_id, l.locator"
        "  FROM receipt.receipts r"
        "  JOIN receipt.stored_files f ON f.id = r.rendition_file_id"
        "  JOIN receipt.file_locations l"
        "    ON l.file_id = f.id AND l.receipt_id = r.id"
        " WHERE r.id = %s",
        (receipt_id,),
    ).fetchone()
    conn.rollback()
    assert row is not None
    return row


def _status(conn: Conn, file_id: Any) -> str | None:
    row = conn.execute(
        "SELECT pdf_status FROM receipt.stored_files WHERE id = %s", (file_id,)
    ).fetchone()
    conn.rollback()
    assert row is not None
    return row["pdf_status"]  # type: ignore[no-any-return]


def _attachment(conn: Conn, message_id: str) -> dict[str, Any]:
    files = _linked_files(conn, _provenance(conn, message_id)["id"])
    message, attachment = files
    assert _status(conn, message["file_id"]) is None  # message.eml isn't a PDF
    return attachment


def test_an_ok_pdf_is_its_own_rendition(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user = _unique_user("pdfok")
    pdf = make_pdf(f"Clean {uuid.uuid4().hex}")
    message_id = _seed(user, pdf)

    (receipt,) = _ingest(pg_conn, user, LocalEvidenceStore(tmp_path), "pdf-ok").receipts

    attachment = _attachment(pg_conn, message_id)
    rendition = _rendition(pg_conn, receipt.id)
    assert rendition["id"] == attachment["file_id"]  # one row, shared
    assert rendition["pdf_status"] == "ok"
    assert rendition["repaired_from_id"] is None
    assert (tmp_path / rendition["locator"]).read_bytes() == pdf


def test_a_repaired_pdf_is_kept_as_sent_and_its_repair_is_the_rendition(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user = _unique_user("pdfrep")
    damaged = truncated_pdf(f"Damaged {uuid.uuid4().hex}")
    message_id = _seed(user, damaged)

    (receipt,) = _ingest(
        pg_conn, user, LocalEvidenceStore(tmp_path), "pdf-rep"
    ).receipts

    attachment = _attachment(pg_conn, message_id)
    assert _status(pg_conn, attachment["file_id"]) == "repaired"
    # The original is never changed.
    assert (tmp_path / attachment["locator"]).read_bytes() == damaged
    assert bytes(attachment["sha256"]) == hashlib.sha256(damaged).digest()

    rendition = _rendition(pg_conn, receipt.id)
    assert rendition["id"] != attachment["file_id"]
    assert rendition["pdf_status"] == "ok"
    assert rendition["repaired_from_id"] == attachment["file_id"]
    repaired = (tmp_path / rendition["locator"]).read_bytes()
    assert repaired == check_pdf(damaged).repaired
    assert bytes(rendition["sha256"]) == hashlib.sha256(repaired).digest()
    assert check_pdf(repaired).status == "ok"


def test_an_unparseable_pdf_is_kept_and_the_body_is_rendered(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user = _unique_user("pdfbad")
    junk = garbage_pdf() + uuid.uuid4().bytes
    message_id = _seed(user, junk, text_body="Total paid: 12.00")

    (receipt,) = _ingest(
        pg_conn, user, LocalEvidenceStore(tmp_path), "pdf-bad"
    ).receipts

    attachment = _attachment(pg_conn, message_id)
    assert _status(pg_conn, attachment["file_id"]) == "unparseable"
    assert (tmp_path / attachment["locator"]).read_bytes() == junk

    rendition = _rendition(pg_conn, receipt.id)
    assert rendition["id"] != attachment["file_id"]
    assert rendition["pdf_status"] == "ok"
    assert rendition["repaired_from_id"] is None
    body = (tmp_path / rendition["locator"]).read_bytes()
    assert body.startswith(b"%PDF-") and body != junk


def test_an_unparseable_pdf_with_no_body_fails_for_retry(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user = _unique_user("pdfnobody")
    message_id = _seed(user, garbage_pdf() + uuid.uuid4().bytes, text_body=None)

    result = _ingest(pg_conn, user, LocalEvidenceStore(tmp_path), "pdf-nobody")

    assert result.receipts == []
    row = pg_conn.execute(
        "SELECT error_class, error_message, next_retry_at FROM receipt.ingest_log"
        " WHERE source_id = %s AND status = 'failed'",
        (message_id,),
    ).fetchone()
    receipts = pg_conn.execute(
        "SELECT count(*) AS n FROM receipt.receipts WHERE source_id = %s",
        (message_id,),
    ).fetchone()
    pg_conn.rollback()
    assert row is not None
    assert row["error_class"] == "content"
    assert "no body to render" in row["error_message"]
    assert row["next_retry_at"] is not None
    assert receipts == {"n": 0}


def test_a_repaired_drive_pdf_points_at_a_source_row_with_no_location(
    pg_conn: Conn, tmp_path: Path
) -> None:
    damaged = truncated_pdf(f"Drive {uuid.uuid4().hex}")
    raw = RawReceipt(
        source_id=f"drive-{uuid.uuid4().hex}",
        source_name="drive-pdf",
        source_type="gdrive",
        date=datetime(2026, 10, 14, tzinfo=UTC),
        file_name="scan.pdf",
        file_content=damaged,
        file_content_type="application/pdf",
        raw_bytes=damaged,
    )

    outcome = process_item(
        pg_conn,
        raw,
        store=LocalEvidenceStore(tmp_path),
        source_name="drive-pdf",
        source_type="gdrive",
        llm_model="test-model",
        agent=_agent(None),
    )

    assert outcome.status == "success" and outcome.receipt is not None
    rendition = _rendition(pg_conn, outcome.receipt.id)
    assert rendition["pdf_status"] == "ok"
    source = pg_conn.execute(
        "SELECT f.sha256, f.pdf_status, count(l.id) AS locations"
        "  FROM receipt.stored_files f"
        "  LEFT JOIN receipt.file_locations l ON l.file_id = f.id"
        " WHERE f.id = %s GROUP BY f.id",
        (rendition["repaired_from_id"],),
    ).fetchone()
    pg_conn.rollback()
    assert source is not None
    assert bytes(source["sha256"]) == hashlib.sha256(damaged).digest()
    assert source["pdf_status"] == "repaired"
    assert source["locations"] == 0
