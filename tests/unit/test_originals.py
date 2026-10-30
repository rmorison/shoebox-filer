"""Tests for receipt_index.originals and the pipeline's original-keeping step.

Storing runs against a real ``LocalEvidenceStore`` under ``tmp_path``; the
database lookup for bytes the store already holds is replaced by a dict.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from receipt_index import originals, pipeline
from receipt_index.models import (
    Attachment,
    ExtractionResult,
    FieldConfidence,
    RawReceipt,
    Receipt,
    ReceiptMetadata,
)
from receipt_index.originals import (
    MESSAGE_MEDIA_TYPE,
    StoredAttachment,
    StoredEmail,
    attachment_name,
    select_receipt_document,
    store_email_original,
)
from receipt_index.store import LocalEvidenceStore, StoredFile

if TYPE_CHECKING:
    from pathlib import Path

_RECEIVED = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
_EML = b"Message-ID: <a@example.com>\r\nSubject: Receipt\r\n\r\nTotal: 12.00\r\n"


def _att(
    filename: str | None,
    content_type: str = "application/pdf",
    data: bytes = b"%PDF-1.4 x",
) -> Attachment:
    return Attachment(
        filename=filename or "unnamed",
        content_type=content_type,
        data=data,
        mime_filename=filename,
    )


def _raw(
    attachments: list[Attachment] | None = None,
    *,
    raw_bytes: bytes | None = _EML,
    source_type: str = "imap",
) -> RawReceipt:
    return RawReceipt(
        source_id="<a@example.com>",
        source_name="mail-receipts",
        source_type=source_type,
        date=_RECEIVED,
        subject="Receipt",
        sender="shop@example.com",
        text_body="Total: 12.00",
        attachments=attachments or [],
        raw_bytes=raw_bytes,
    )


# ---------------------------------------------------------------------------
# Attachment names
# ---------------------------------------------------------------------------


class TestAttachmentName:
    def test_position_slug_and_extension(self) -> None:
        name = attachment_name(1, _att("Receipt #123.PDF"))
        assert name == "01__receipt-123.pdf"

    @pytest.mark.parametrize(
        ("filename", "content_type", "expected"),
        [
            ("../../etc/passwd", "application/pdf", "02__passwd.pdf"),
            ("..\\..\\boot.ini", "text/plain", "02__boot.txt"),
            ("in\x00voi\nce\x7f.pdf", "application/pdf", "02__in-voi-ce.pdf"),
            ("no-extension", "image/png", "02__no-extension.png"),
            ("evil.exe", "application/x-msdownload", "02__evil.bin"),
            ("photo.jpeg", "image/jpeg; name=photo.jpeg", "02__photo.jpg"),
            ("....", "application/pdf", "02__attachment.pdf"),
        ],
    )
    def test_built_never_taken_from_the_sender(
        self, filename: str, content_type: str, expected: str
    ) -> None:
        assert attachment_name(2, _att(filename, content_type)) == expected

    def test_a_long_name_is_cut(self) -> None:
        name = attachment_name(3, _att("x" * 1000 + ".pdf"))
        stem = name.removeprefix("03__").removesuffix(".pdf")
        assert 0 < len(stem) <= 60


# ---------------------------------------------------------------------------
# Receipt-document selection (evidence-store design, section 2)
# ---------------------------------------------------------------------------


class TestSelectReceiptDocument:
    def test_a_pdf_the_extraction_names_is_certain(self) -> None:
        atts = [_att("terms.pdf"), _att("receipt.pdf")]
        assert select_receipt_document("receipt.pdf", atts) == (1, "certain")

    def test_a_lone_pdf_the_extraction_doesnt_name_is_ambiguous(self) -> None:
        assert select_receipt_document(None, [_att("terms.pdf")]) == (0, "ambiguous")

    def test_several_pdfs_unnamed_take_the_first_ambiguous(self) -> None:
        atts = [_att("logo.png", "image/png"), _att("a.pdf"), _att("b.pdf")]
        assert select_receipt_document(None, atts) == (1, "ambiguous")

    def test_a_name_shared_by_two_attachments_is_ambiguous(self) -> None:
        atts = [_att("receipt.pdf"), _att("receipt.pdf")]
        assert select_receipt_document("receipt.pdf", atts) == (0, "ambiguous")

    def test_a_name_matching_none_is_ambiguous(self) -> None:
        atts = [_att("receipt.pdf")]
        assert select_receipt_document("other.pdf", atts) == (0, "ambiguous")

    def test_a_name_the_extractor_cleaned_matches_none(self) -> None:
        atts = [_att("receipt---copy.pdf")]
        assert select_receipt_document("receipt-copy.pdf", atts) == (0, "ambiguous")

    def test_body_only_has_no_receipt_document(self) -> None:
        atts = [_att("terms.pdf")]
        assert select_receipt_document("body", atts) == (None, None)

    def test_a_logo_only_email_has_no_receipt_document(self) -> None:
        atts = [_att("logo.png", "image/png"), _att(None, "image/gif")]
        assert select_receipt_document(None, atts) == (None, None)

    def test_a_photographed_receipt_the_extraction_names_is_certain(self) -> None:
        atts = [_att("logo.png", "image/png"), _att("IMG_0042.jpg", "image/jpeg")]
        assert select_receipt_document("IMG_0042.jpg", atts) == (1, "certain")

    def test_an_unnamed_part_is_never_certain(self) -> None:
        # The extractor sees the stand-in name, but it isn't a MIME file name.
        atts = [_att(None, "image/png")]
        assert select_receipt_document("unnamed", atts) == (None, None)

    def test_no_attachments(self) -> None:
        assert select_receipt_document(None, []) == (None, None)


# ---------------------------------------------------------------------------
# store_email_original
# ---------------------------------------------------------------------------


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> dict[bytes, str]:
    """Originals the store already holds, by sha256 (the database lookup)."""
    known: dict[bytes, str] = {}
    monkeypatch.setattr(
        originals, "store_original_ref", lambda conn, sha256: known.get(sha256)
    )
    return known


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


class TestStoreEmailOriginal:
    def test_stores_the_message_and_attachments_in_one_folder(
        self, tmp_path: Path, held: dict[bytes, str]
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        raw = _raw(
            [_att("receipt.pdf"), _att("logo.png", "image/png", b"\x89PNG logo")]
        )
        email = store_email_original(
            MagicMock(), store, raw, source_name="Mail Receipts", received_at=_RECEIVED
        )

        prefix = hashlib.sha256(_EML).hexdigest()[:12]
        folder = f"originals/2026/10/2026-10-14__mail-receipts__{prefix}"
        assert _files(tmp_path) == [
            f"{folder}/01__receipt.pdf",
            f"{folder}/02__logo.png",
            f"{folder}/message.eml",
        ]
        assert (tmp_path / folder / "message.eml").read_bytes() == _EML
        assert email.message.ref == f"local:{folder}/message.eml"
        assert email.message.media_type == MESSAGE_MEDIA_TYPE
        assert email.message.sha256 == hashlib.sha256(_EML).digest()
        assert [a.file_name for a in email.attachments] == ["receipt.pdf", "logo.png"]
        assert [a.file.media_type for a in email.attachments] == [
            "application/pdf",
            "image/png",
        ]

    def test_a_second_store_writes_nothing(
        self, tmp_path: Path, held: dict[bytes, str]
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        raw = _raw([_att("receipt.pdf")])
        first = store_email_original(
            MagicMock(), store, raw, source_name="mail", received_at=_RECEIVED
        )
        before = {
            p: (p.stat().st_ino, p.stat().st_mtime_ns)
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        second = store_email_original(
            MagicMock(), store, raw, source_name="mail", received_at=_RECEIVED
        )
        after = {
            p: (p.stat().st_ino, p.stat().st_mtime_ns)
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        assert second == first
        assert after == before

    def test_bytes_the_store_holds_are_reused_not_written(
        self, tmp_path: Path, held: dict[bytes, str]
    ) -> None:
        terms = b"%PDF-1.4 terms and conditions"
        held[hashlib.sha256(terms).digest()] = "local:originals/elsewhere/01__t.pdf"
        (tmp_path / "originals" / "elsewhere").mkdir(parents=True)
        (tmp_path / "originals" / "elsewhere" / "01__t.pdf").write_bytes(terms)
        store = LocalEvidenceStore(tmp_path)
        email = store_email_original(
            MagicMock(),
            store,
            _raw([_att("terms.pdf", data=terms)]),
            source_name="mail",
            received_at=_RECEIVED,
        )
        (att,) = email.attachments
        assert att.file.ref == "local:originals/elsewhere/01__t.pdf"
        assert att.file.sha256 == hashlib.sha256(terms).digest()
        assert [f for f in _files(tmp_path) if f.endswith(".pdf")] == [
            "originals/elsewhere/01__t.pdf"
        ]

    def test_a_recorded_original_missing_from_the_store_is_written_again(
        self, tmp_path: Path, held: dict[bytes, str]
    ) -> None:
        terms = b"%PDF-1.4 terms and conditions"
        held[hashlib.sha256(terms).digest()] = "local:originals/gone/01__t.pdf"
        email = store_email_original(
            MagicMock(),
            LocalEvidenceStore(tmp_path),
            _raw([_att("terms.pdf", data=terms)]),
            source_name="mail",
            received_at=_RECEIVED,
        )
        (att,) = email.attachments
        assert att.file.ref.endswith("/01__terms.pdf")
        assert (tmp_path / att.file.ref.removeprefix("local:")).read_bytes() == terms

    def test_another_backends_original_isnt_reused_here(
        self, tmp_path: Path, held: dict[bytes, str]
    ) -> None:
        terms = b"%PDF-1.4 terms and conditions"
        held[hashlib.sha256(terms).digest()] = "drive:file-id"
        email = store_email_original(
            MagicMock(),
            LocalEvidenceStore(tmp_path),
            _raw([_att("terms.pdf", data=terms)]),
            source_name="mail",
            received_at=_RECEIVED,
        )
        assert email.attachments[0].file.ref.startswith("local:originals/")

    def test_hostile_names_stay_inside_the_root(
        self, tmp_path: Path, held: dict[bytes, str]
    ) -> None:
        root = tmp_path / "store"
        root.mkdir()
        store = LocalEvidenceStore(root)
        atts = [
            _att("../../../outside.pdf", data=b"%PDF a"),
            _att("x\x01\x02.pdf", data=b"%PDF b"),
            _att("y" * 5000 + ".pdf", data=b"%PDF c"),
        ]
        email = store_email_original(
            MagicMock(), store, _raw(atts), source_name="mail", received_at=_RECEIVED
        )
        assert [p.name for p in tmp_path.iterdir()] == ["store"]
        for att in email.attachments:
            assert store.local_path(att.file.ref).is_relative_to(root.resolve())
        assert [a.file_name for a in email.attachments] == [a.filename for a in atts]

    def test_refuses_an_item_without_raw_bytes(
        self, tmp_path: Path, held: dict[bytes, str]
    ) -> None:
        with pytest.raises(ValueError, match="no raw bytes"):
            store_email_original(
                MagicMock(),
                LocalEvidenceStore(tmp_path),
                _raw(raw_bytes=None),
                source_name="mail",
                received_at=_RECEIVED,
            )


# ---------------------------------------------------------------------------
# The pipeline: record_ingest_evidence and process_item
# ---------------------------------------------------------------------------

_RECEIPT = Receipt.model_validate(
    {
        "id": UUID("019572a0-0000-7000-8000-000000000001"),
        "source_id": "<a@example.com>",
        "source_type": "imap",
        "source_name": "mail",
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("12.00"),
        "currency": "USD",
        "receipt_date": date(2026, 10, 14),
        "description": "Parking",
        "confidence": 0.95,
        "pdf_path": "renditions/2026/10/r.pdf",
        "email_subject": "Receipt",
        "email_sender": "shop@example.com",
        "email_date": _RECEIVED,
        "file_name": None,
        "created_at": _RECEIVED,
        "updated_at": _RECEIVED,
    }
)
_RENDITION = StoredFile(
    sha256=b"\x01" * 32,
    byte_size=4,
    media_type="application/pdf",
    ref="local:renditions/2026/10/r.pdf",
)


def _stored(name: str) -> StoredFile:
    return StoredFile(
        sha256=hashlib.sha256(name.encode()).digest(),
        byte_size=1,
        media_type="application/pdf",
        ref=f"local:originals/x/{name}",
    )


class _Evidence:
    """Captures what record_ingest_evidence writes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.provenance: dict[str, Any] = {}
        self.files: list[Any] = []
        self.provenance_id = uuid4()
        monkeypatch.setattr(pipeline, "insert_provenance", self._provenance)
        monkeypatch.setattr(
            pipeline,
            "insert_provenance_files",
            lambda conn, pid, files: self.files.extend((pid, *f) for f in files),
        )
        monkeypatch.setattr(pipeline, "record_rendition", MagicMock())
        self.ids: dict[str, UUID] = {}

        def record(conn: Any, email: StoredEmail) -> StoredEmail:
            def with_id(f: StoredFile) -> StoredFile:
                self.ids[f.ref] = uuid4()
                return StoredFile(
                    sha256=f.sha256,
                    byte_size=f.byte_size,
                    media_type=f.media_type,
                    ref=f.ref,
                    id=self.ids[f.ref],
                )

            return StoredEmail(
                message=with_id(email.message),
                attachments=[
                    StoredAttachment(file=with_id(a.file), file_name=a.file_name)
                    for a in email.attachments
                ],
            )

        monkeypatch.setattr(originals, "record_email_original", record)

    def _provenance(self, conn: Any, **kwargs: Any) -> Any:
        self.provenance = kwargs
        return MagicMock(id=self.provenance_id)


class TestRecordIngestEvidence:
    def test_an_email_records_its_original_and_receipt_document(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ev = _Evidence(monkeypatch)
        raw = _raw([_att("terms.pdf"), _att("receipt.pdf")])
        email = StoredEmail(
            message=_stored("message.eml"),
            attachments=[
                StoredAttachment(file=_stored("01__terms.pdf"), file_name="terms.pdf"),
                StoredAttachment(
                    file=_stored("02__receipt.pdf"), file_name="receipt.pdf"
                ),
            ],
        )
        pipeline.record_ingest_evidence(
            MagicMock(),
            _RECEIPT,
            raw,
            _RENDITION,
            source_name="mail",
            source_type="imap",
            email=email,
            receipt_document="receipt.pdf",
        )

        message_id = ev.ids["local:originals/x/message.eml"]
        receipt_id = ev.ids["local:originals/x/02__receipt.pdf"]
        assert ev.provenance["source_sha256"] == hashlib.sha256(_EML).digest()
        assert ev.provenance["original_file_id"] == message_id
        assert ev.provenance["receipt_document_file_id"] == receipt_id
        assert ev.provenance["document_certainty"] == "certain"
        assert ev.files == [
            (ev.provenance_id, message_id, "message", None),
            (
                ev.provenance_id,
                ev.ids["local:originals/x/01__terms.pdf"],
                "attachment",
                "terms.pdf",
            ),
            (ev.provenance_id, receipt_id, "attachment", "receipt.pdf"),
        ]

    def test_a_nul_in_a_raw_name_is_recorded_as_a_replacement_character(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ev = _Evidence(monkeypatch)
        name = "receipt\x00.pdf"
        raw = _raw([_att(name)])
        email = StoredEmail(
            message=_stored("message.eml"),
            attachments=[StoredAttachment(file=_stored("01__r.pdf"), file_name=name)],
        )
        pipeline.record_ingest_evidence(
            MagicMock(),
            _RECEIPT,
            raw,
            _RENDITION,
            source_name="mail",
            source_type="imap",
            email=email,
            receipt_document=name,
        )
        assert ev.files[1][3] == "receipt\ufffd.pdf"
        # Matching still uses the name as sent.
        assert ev.provenance["document_certainty"] == "certain"

    def test_a_drive_item_is_pending_with_its_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ev = _Evidence(monkeypatch)
        raw = _raw(raw_bytes=b"%PDF-1.4 scan", source_type="gdrive")
        pipeline.record_ingest_evidence(
            MagicMock(),
            _RECEIPT,
            raw,
            _RENDITION,
            source_name="drive",
            source_type="gdrive",
        )
        assert (
            ev.provenance["source_sha256"] == hashlib.sha256(b"%PDF-1.4 scan").digest()
        )
        assert ev.provenance["original_file_id"] is None
        assert ev.provenance["receipt_document_file_id"] is None
        assert ev.provenance["document_certainty"] is None
        assert ev.files == []

    def test_a_native_doc_has_no_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ev = _Evidence(monkeypatch)
        raw = _raw(raw_bytes=None, source_type="gdrive")
        pipeline.record_ingest_evidence(
            MagicMock(),
            _RECEIPT,
            raw,
            _RENDITION,
            source_name="drive",
            source_type="gdrive",
        )
        assert ev.provenance["source_sha256"] is None


def _extraction(confidence: float = 0.95) -> ExtractionResult:
    return ExtractionResult(
        metadata=ReceiptMetadata(
            vendor="Acme Parking (U.S.), LLC",
            amount=Decimal("12.00"),
            currency="USD",
            date=date(2026, 10, 14),
            description="Parking",
            confidence=confidence,
            field_confidence=FieldConfidence(
                vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9
            ),
            receipt_document="body",
        ),
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        requests=1,
    )


class TestProcessItem:
    @pytest.fixture
    def stub(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
        mocks = {
            name: MagicMock()
            for name in (
                "store_email_original",
                "record_ingest_evidence",
                "insert_ingest_log",
                "insert_extraction",
                "link_by_rule",
            )
        }
        mocks["insert_receipt"] = MagicMock(return_value=_RECEIPT)
        mocks["extract_metadata"] = MagicMock(return_value=_extraction())
        mocks["render_pdf"] = MagicMock(return_value=b"%PDF")
        mocks["store_rendition"] = MagicMock(return_value=_RENDITION)
        for name, mock in mocks.items():
            monkeypatch.setattr(pipeline, name, mock)
        return mocks

    def _process(self, raw: RawReceipt) -> pipeline.ItemOutcome:
        return pipeline.process_item(
            MagicMock(),
            raw,
            store=MagicMock(),
            source_name="mail",
            source_type=raw.source_type,
            llm_model="m",
        )

    def test_an_email_receipt_stores_its_original(
        self, stub: dict[str, MagicMock]
    ) -> None:
        raw = _raw([_att("receipt.pdf")])
        assert self._process(raw).status == "success"
        call = stub["store_email_original"].call_args
        assert call.args[2] is raw
        assert call.kwargs == {"source_name": "mail", "received_at": _RECEIVED}
        evidence = stub["record_ingest_evidence"].call_args.kwargs
        assert evidence["email"] is stub["store_email_original"].return_value
        assert evidence["receipt_document"] == "body"

    def test_a_skipped_item_stores_no_original(
        self, stub: dict[str, MagicMock]
    ) -> None:
        stub["extract_metadata"].return_value = _extraction(confidence=0.1)
        assert self._process(_raw([_att("x.pdf")])).status == "skipped"
        stub["store_email_original"].assert_not_called()

    def test_a_drive_item_stores_no_original(self, stub: dict[str, MagicMock]) -> None:
        raw = _raw(raw_bytes=b"%PDF scan", source_type="gdrive")
        assert self._process(raw).status == "success"
        stub["store_email_original"].assert_not_called()
        assert stub["record_ingest_evidence"].call_args.kwargs["email"] is None

    def test_an_email_without_raw_bytes_stays_pending(
        self, stub: dict[str, MagicMock], caplog: pytest.LogCaptureFixture
    ) -> None:
        assert self._process(_raw(raw_bytes=None)).status == "success"
        stub["store_email_original"].assert_not_called()
        assert stub["record_ingest_evidence"].call_args.kwargs["email"] is None
        assert "original stays pending" in caplog.text
