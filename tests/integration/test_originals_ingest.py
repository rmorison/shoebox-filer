"""Ingest keeps each email's original on the local store (evidence-store ticket 3).

An email that becomes a receipt has its ``message.eml`` (the bytes the IMAP
server served) and each attachment stored under one ``originals/`` folder,
recorded in ``stored_files`` and ``file_locations``, and linked through
``provenance_files``. Storing is idempotent: a retry and a second ingest store
nothing twice, and bytes the store already holds are reused. A Drive item on
the local backend is recorded ``pending`` with its hash, and nothing in Drive
moves.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import psycopg
import pytest
from imapclient import IMAPClient

from receipt_index import pipeline
from receipt_index.adapters.gdrive import GdriveAdapter
from receipt_index.adapters.imap import ImapAdapter
from receipt_index.models import FieldConfidence, ReceiptMetadata
from receipt_index.pipeline import process_item, run_ingest
from receipt_index.repository import OriginalLocationConflictError, record_original
from receipt_index.store import LocalEvidenceStore
from tests.pdfs import make_pdf
from tests.unit.test_gdrive_adapter import (
    FakeDrive,
    _make_file_meta,
    _make_gdrive_config,
    _Req,
)

from .conftest import GREENMAIL_IMAP_PORT, seed_email
from .test_e2e_pipeline import _greenmail_imap_config, _unique_user

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]


def _agent(receipt_document: str | None) -> MagicMock:
    meta = ReceiptMetadata(
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal("12.00"),
        date=date(2026, 10, 14),
        confidence=0.95,
        field_confidence=FieldConfidence(
            vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9
        ),
        receipt_document=receipt_document,
    )
    usage = MagicMock(input_tokens=10, output_tokens=5, cache_read_tokens=0, requests=1)
    agent = MagicMock()
    agent.run_sync.return_value = MagicMock(output=meta, usage=usage)
    return agent


def _ingest(
    conn: Conn,
    user: str,
    store: LocalEvidenceStore,
    source: str,
    receipt_document: str | None = None,
) -> Any:
    config = _greenmail_imap_config(user).model_copy(update={"name": source})
    return run_ingest(
        conn=conn,
        adapter=ImapAdapter(config),
        store=store,
        source_name=source,
        source_type="imap",
        agent=_agent(receipt_document),
        llm_model="test-model",
    )


def _served_bytes(user: str) -> bytes:
    """The one message in the user's INBOX, as the IMAP server serves it."""
    with IMAPClient("localhost", port=GREENMAIL_IMAP_PORT, ssl=False) as client:
        client.login(user, "any")
        client.select_folder("INBOX", readonly=True)
        (uid,) = client.search("ALL")
        data: bytes = client.fetch([uid], ["BODY.PEEK[]"])[uid][b"BODY[]"]
    return data


def _pdf(label: str) -> bytes:
    """A valid PDF, unique to this call, so it passes through as the rendition."""
    return make_pdf(f"{label} {uuid.uuid4().hex}")


def _files(root: Path) -> dict[str, tuple[int, int]]:
    """Every stored file under ``originals/``, with its inode and mtime."""
    return {
        p.relative_to(root).as_posix(): (p.stat().st_ino, p.stat().st_mtime_ns)
        for p in (root / "originals").rglob("*")
        if p.is_file() and not p.name.startswith(".")
    }


def _provenance(conn: Conn, source_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM receipt.provenance WHERE source_id = %s", (source_id,)
    ).fetchone()
    conn.rollback()
    assert row is not None
    return row


def _linked_files(conn: Conn, provenance_id: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT pf.position, pf.role, pf.file_name, pf.file_id, f.sha256,"
        "       f.media_type, l.locator, l.owner"
        "  FROM receipt.provenance_files pf"
        "  JOIN receipt.stored_files f ON f.id = pf.file_id"
        "  JOIN receipt.file_locations l"
        "    ON l.file_id = f.id AND l.purpose = 'original'"
        " WHERE pf.provenance_id = %s ORDER BY pf.position, l.created_at",
        (provenance_id,),
    ).fetchall()
    conn.rollback()
    return rows


def _rows_for(conn: Conn, data: bytes) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM receipt.stored_files WHERE sha256 = %s",
        (hashlib.sha256(data).digest(),),
    ).fetchone()
    conn.rollback()
    assert row is not None
    return int(row["n"])


def test_an_email_keeps_its_original_attachments_and_rendition(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user = _unique_user("orig")
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    receipt_pdf, logo = _pdf("receipt"), b"\x89PNG\r\n" + uuid.uuid4().bytes
    seed_email(
        to_addr=user,
        subject="Your parking receipt",
        text_body="Receipt attached.",
        message_id=message_id,
        attachments=[
            ("Receipt 2026-10.pdf", "application/pdf", receipt_pdf),
            ("logo.png", "image/png", logo),
        ],
    )
    served = _served_bytes(user)
    store = LocalEvidenceStore(tmp_path)

    result = _ingest(pg_conn, user, store, "mail-orig", "Receipt 2026-10.pdf")

    (receipt,) = result.receipts
    prov = _provenance(pg_conn, message_id)
    assert prov["original_status"] == "stored"
    assert bytes(prov["source_sha256"]) == hashlib.sha256(served).digest()
    files = _linked_files(pg_conn, prov["id"])
    assert [(f["position"], f["role"], f["file_name"]) for f in files] == [
        (0, "message", None),
        (1, "attachment", "Receipt 2026-10.pdf"),
        (2, "attachment", "logo.png"),
    ]
    message, pdf, image = files
    assert prov["original_file_id"] == message["file_id"]
    assert prov["receipt_document_file_id"] == pdf["file_id"]
    assert prov["document_certainty"] == "certain"
    assert {f["owner"] for f in files} == {"store"}
    assert [f["media_type"] for f in files] == [
        "message/rfc822",
        "application/pdf",
        "image/png",
    ]

    # Byte for byte what the server served, and each hash is its file's.
    folder = message["locator"].rsplit("/", 1)[0]
    assert folder.startswith("originals/")
    assert folder.endswith(hashlib.sha256(served).hexdigest()[:12])
    assert pdf["locator"] == f"{folder}/01__receipt-2026-10.pdf"
    assert image["locator"] == f"{folder}/02__logo.png"
    assert (tmp_path / message["locator"]).read_bytes() == served
    for f, data in ((message, served), (pdf, receipt_pdf), (image, logo)):
        assert (tmp_path / f["locator"]).read_bytes() == data
        assert bytes(f["sha256"]) == hashlib.sha256(data).digest()

    # The PDF passed through as the rendition: one stored file, two locations.
    rendition = pg_conn.execute(
        "SELECT rendition_file_id FROM receipt.receipts WHERE id = %s",
        (receipt.id,),
    ).fetchone()
    pg_conn.rollback()
    assert rendition == {"rendition_file_id": pdf["file_id"]}


def test_a_retry_after_a_failed_commit_stores_nothing_twice(
    greenmail_available: bool,
    pg_conn: Conn,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _unique_user("orig-retry")
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    terms = _pdf("terms")
    seed_email(
        to_addr=user,
        subject="Your parking receipt",
        text_body="Total: 12.00",
        message_id=message_id,
        attachments=[("terms.pdf", "application/pdf", terms)],
    )
    store = LocalEvidenceStore(tmp_path)
    with ImapAdapter(_greenmail_imap_config(user)) as adapter:
        raw = adapter.fetch_by_id(message_id)
    assert raw is not None

    def fail(conn: Conn, receipt: Any) -> list[Any]:
        msg = "the database went away mid-commit"
        raise RuntimeError(msg)

    def process() -> str:
        assert raw is not None
        return process_item(
            pg_conn,
            raw,
            store=store,
            source_name="mail-retry",
            source_type="imap",
            agent=_agent("body"),
            llm_model="test-model",
        ).status

    monkeypatch.setattr(pipeline, "link_by_rule", fail)
    assert process() == "failed"
    stored = _files(tmp_path)
    assert len(stored) == 2  # message.eml and the attachment, no rows
    assert raw.raw_bytes is not None
    assert _rows_for(pg_conn, raw.raw_bytes) == 0

    monkeypatch.undo()
    assert process() == "success"

    assert _files(tmp_path) == stored  # nothing written again
    assert _rows_for(pg_conn, raw.raw_bytes) == 1
    assert _rows_for(pg_conn, terms) == 1
    prov = _provenance(pg_conn, message_id)
    # "body": the receipt is the email itself, so no receipt document.
    assert prov["receipt_document_file_id"] is None
    assert prov["document_certainty"] is None
    assert len(_linked_files(pg_conn, prov["id"])) == 2


def test_the_same_message_again_stores_nothing(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    first, second = _unique_user("orig-a"), _unique_user("orig-b")
    pdf = _pdf("receipt")
    for user in (first, second):
        seed_email(
            to_addr=user,
            subject="Your parking receipt",
            message_id=message_id,
            pdf_attachment=("receipt.pdf", pdf),
        )
    store = LocalEvidenceStore(tmp_path)

    assert _ingest(pg_conn, first, store, "mail-a").processed == 1
    stored = _files(tmp_path)
    again = _ingest(pg_conn, second, store, "mail-b")

    assert again.processed == 0
    assert _files(tmp_path) == stored
    assert _rows_for(pg_conn, pdf) == 1


def test_two_emails_share_one_copy_of_the_same_attachment(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    terms = _pdf("terms and conditions")
    users = [_unique_user("orig-t1"), _unique_user("orig-t2")]
    ids = [f"<{uuid.uuid4().hex}@example.com>" for _ in users]
    store = LocalEvidenceStore(tmp_path)
    for user, message_id in zip(users, ids, strict=True):
        seed_email(
            to_addr=user,
            subject="Your parking receipt",
            text_body=f"Total: 12.00 ({message_id})",
            message_id=message_id,
            attachments=[("terms.pdf", "application/pdf", terms)],
        )
        assert _ingest(pg_conn, user, store, f"mail-{user[:10]}", "body").processed == 1

    assert _rows_for(pg_conn, terms) == 1
    on_disk = [p for p in _files(tmp_path) if p.endswith("__terms.pdf")]
    assert len(on_disk) == 1
    linked = [
        _linked_files(pg_conn, _provenance(pg_conn, m)["id"])[1]["file_id"] for m in ids
    ]
    assert linked[0] == linked[1]


def test_an_operator_owned_copy_doesnt_count(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    """Bytes held only in an adopted dropoff file get their own store copy."""
    terms = _pdf("terms")
    file_id = pg_conn.execute(
        "INSERT INTO receipt.stored_files (sha256, byte_size, media_type)"
        " VALUES (%s, %s, 'application/pdf') RETURNING id",
        (hashlib.sha256(terms).digest(), len(terms)),
    ).fetchone()
    assert file_id is not None
    pg_conn.execute(
        "INSERT INTO receipt.file_locations"
        " (file_id, backend, locator, purpose, owner, display_path)"
        " VALUES (%s, 'drive', %s, 'original', 'operator', 'dropoff/terms.pdf')",
        (file_id["id"], f"drive-{uuid.uuid4().hex}"),
    )
    pg_conn.commit()
    user = _unique_user("orig-op")
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    seed_email(
        to_addr=user,
        subject="Your parking receipt",
        text_body="Total: 12.00",
        message_id=message_id,
        attachments=[("terms.pdf", "application/pdf", terms)],
    )

    assert (
        _ingest(pg_conn, user, LocalEvidenceStore(tmp_path), "mail-op").processed == 1
    )

    attachment = _linked_files(pg_conn, _provenance(pg_conn, message_id)["id"])
    locations = [(f["owner"], f["locator"]) for f in attachment if f["position"] == 1]
    assert ("operator", locations[0][1]) in locations
    (own,) = [loc for owner, loc in locations if owner == "store"]
    assert own.endswith("/01__terms.pdf")
    assert (tmp_path / own).read_bytes() == terms
    assert _rows_for(pg_conn, terms) == 1


class _ReadOnlyDrive(FakeDrive):
    """The Drive service, with every call it doesn't serve recorded.

    It serves reads only, so a move, copy, update or delete would show up in
    ``unserved`` (and fail the item).
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.unserved: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.unserved.append(name)
        raise AttributeError(name)

    def get_media(self, fileId: str) -> _Req:  # noqa: N803 (Drive API name)
        # A valid PDF, as ingest now validates a Drive PDF (design §9).
        return _Req(lambda: make_pdf(fileId))


def test_a_drive_item_on_the_local_backend_is_pending(
    pg_conn: Conn, tmp_path: Path
) -> None:
    file_id = f"drive-{uuid.uuid4().hex}"
    source = f"drive-{uuid.uuid4().hex[:8]}"
    drive = _ReadOnlyDrive(files=[_make_file_meta(file_id=file_id)])
    adapter = GdriveAdapter(_make_gdrive_config(name=source), settle_seconds=60)
    adapter._service = drive
    store = LocalEvidenceStore(tmp_path)

    result = run_ingest(
        conn=pg_conn,
        adapter=adapter,
        store=store,
        source_name=source,
        source_type="gdrive",
        agent=_agent(None),
        llm_model="test-model",
    )

    assert result.processed == 1
    assert drive.unserved == []
    prov = _provenance(pg_conn, file_id)
    expected = hashlib.sha256(make_pdf(file_id)).digest()
    assert bytes(prov["source_sha256"]) == expected
    assert prov["original_status"] == "pending"
    assert prov["original_file_id"] is None
    assert prov["receipt_document_file_id"] is None
    assert prov["document_certainty"] is None
    assert _linked_files(pg_conn, prov["id"]) == []
    assert not (tmp_path / "originals").exists()


def test_one_email_carrying_the_same_bytes_twice(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    """Two attachments with identical bytes: one file, one location."""
    user = _unique_user("orig-twice")
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    pdf = _pdf("twice")
    seed_email(
        to_addr=user,
        subject="Your parking receipt",
        text_body="Total: 12.00",
        message_id=message_id,
        attachments=[
            ("receipt.pdf", "application/pdf", pdf),
            ("copy.pdf", "application/pdf", pdf),
        ],
    )

    store = LocalEvidenceStore(tmp_path)
    assert _ingest(pg_conn, user, store, "mail-twice", "receipt.pdf").processed == 1

    prov = _provenance(pg_conn, message_id)
    files = [f for f in _linked_files(pg_conn, prov["id"]) if f["position"] > 0]
    assert {f["file_id"] for f in files} == {files[0]["file_id"]}
    # Written once, under the first name: one location serves both positions.
    assert [(f["position"], f["locator"].rsplit("/", 1)[1]) for f in files] == [
        (1, "01__receipt.pdf"),
        (2, "01__receipt.pdf"),
    ]
    assert [f["file_name"] for f in files] == ["receipt.pdf", "copy.pdf"]
    assert not [p for p in _files(tmp_path) if p.endswith("__copy.pdf")]
    assert _rows_for(pg_conn, pdf) == 1
    assert prov["receipt_document_file_id"] == files[0]["file_id"]
    assert prov["document_certainty"] == "certain"


@pytest.mark.parametrize("held_by", ["other bytes", "a rendition"])
def test_record_original_refuses_a_locator_held_for_something_else(
    pg_conn: Conn, held_by: str
) -> None:
    held, wanted = _pdf("held"), _pdf("wanted")
    locator = f"originals/test/{uuid.uuid4().hex}.pdf"
    file_id = pg_conn.execute(
        "INSERT INTO receipt.stored_files (sha256, byte_size, media_type)"
        " VALUES (%s, %s, 'application/pdf') RETURNING id",
        (hashlib.sha256(held if held_by == "other bytes" else wanted).digest(), 1),
    ).fetchone()
    assert file_id is not None
    if held_by == "other bytes":
        pg_conn.execute(
            "INSERT INTO receipt.file_locations"
            " (file_id, backend, locator, purpose, display_path)"
            " VALUES (%s, 'local', %s, 'original', %s)",
            (file_id["id"], locator, locator),
        )
    else:
        receipt = pg_conn.execute(
            "SELECT id FROM receipt.receipts LIMIT 1"
        ).fetchone() or _seed_receipt(pg_conn)
        pg_conn.execute(
            "INSERT INTO receipt.file_locations"
            " (file_id, backend, locator, purpose, receipt_id, display_path)"
            " VALUES (%s, 'local', %s, 'rendition', %s, %s)",
            (file_id["id"], locator, receipt["id"], locator),
        )

    with pytest.raises(OriginalLocationConflictError):
        record_original(
            pg_conn,
            ref=f"local:{locator}",
            sha256=hashlib.sha256(wanted).digest(),
            byte_size=len(wanted),
            media_type="application/pdf",
        )
    pg_conn.rollback()


def _seed_receipt(conn: Conn) -> dict[str, Any]:
    from receipt_index.repository import insert_receipt

    receipt = insert_receipt(
        conn,
        source_id=f"seed-{uuid.uuid4().hex}",
        source_type="imap",
        source_name="seed",
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal("12.00"),
        currency="USD",
        receipt_date=date(2026, 10, 14),
        description=None,
        confidence=0.9,
        pdf_path=f"2026/10/{uuid.uuid4().hex}.pdf",
        email_subject=None,
        email_sender=None,
        email_date=None,
        commit=False,
    )
    return {"id": receipt.id}
