"""Originals: an email's ``.eml`` and attachments, kept in the evidence store.

``store_email_original`` writes the files under one ``originals/`` folder;
``record_email_original`` records them in the item's own transaction
(evidence-store design, sections 2, 5 and 7). Storing is content-addressed
and idempotent: the folder and every name follow from the bytes, so a retry
writes nothing, and bytes the store already holds as an original are reused
rather than written again.

``select_receipt_document`` picks the file the receipt was read from, from the
extraction's ``receipt_document`` (section 2, "The receipt document").
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from slugify import slugify

from receipt_index.repository import record_original, store_original_ref
from receipt_index.store import StoredFile, StoreError, StoreFolder

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    import psycopg

    from receipt_index.models import Attachment, RawReceipt
    from receipt_index.repository import DocumentCertainty, ProvenanceFileRole
    from receipt_index.store import EvidenceStore

MESSAGE_NAME = "message.eml"
MESSAGE_MEDIA_TYPE = "message/rfc822"

# The stored extension, keyed on the base media type. Anything else is "bin":
# a sender's file name never chooses an extension.
_EXTENSIONS: dict[str, str] = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "text/html": "html",
    "text/plain": "txt",
}
_STEM_MAX = 60
_PDF = "application/pdf"


@dataclass(frozen=True)
class StoredAttachment:
    """One stored attachment and the raw MIME file name it was sent with."""

    file: StoredFile
    file_name: str | None


@dataclass(frozen=True)
class StoredEmail:
    """An email's stored original: the message, then its attachments.

    The files' ``id`` is None until ``record_email_original`` records them.
    """

    message: StoredFile
    attachments: list[StoredAttachment]


def base_media_type(content_type: str) -> str:
    """A MIME type's lowercase base, without parameters."""
    return content_type.split(";")[0].strip().lower()


def attachment_name(position: int, attachment: Attachment) -> str:
    """``{position:02d}__{stem slug, at most 60 chars}.{ext}``.

    Built, never taken from the sender: the stem is slugified from the file
    name and the extension comes from the media type.
    """
    stem = PurePosixPath(attachment.filename.replace("\\", "/")).stem
    slug = slugify(stem, max_length=_STEM_MAX) or "attachment"
    ext = _EXTENSIONS.get(base_media_type(attachment.content_type), "bin")
    return f"{position:02d}__{slug}.{ext}"


def select_receipt_document(
    receipt_document: str | None, attachments: Sequence[Attachment]
) -> tuple[int | None, DocumentCertainty | None]:
    """The attachment the receipt was read from, and how sure that is.

    Returns the attachment's index (or None) and its certainty:

    - a name matching exactly one attachment's MIME file name: ``certain``;
    - a name shared by several: the first of them, ``ambiguous``;
    - a name matching none, or null: the first PDF, ``ambiguous``, since
      a lone PDF can be only terms and conditions; with no PDF, nothing;
    - ``"body"``: nothing (the receipt is the email itself).

    An image is the receipt document only when the extraction names it.
    """
    if receipt_document == "body":
        return None, None
    if receipt_document is not None:
        matches = [
            i
            for i, att in enumerate(attachments)
            if att.mime_filename == receipt_document
        ]
        if len(matches) == 1:
            return matches[0], "certain"
        if matches:
            return matches[0], "ambiguous"
    for i, att in enumerate(attachments):
        if base_media_type(att.content_type) == _PDF:
            return i, "ambiguous"
    return None, None


def _present(store: EvidenceStore, ref: str, size: int) -> bool:
    """Whether a recorded original is still in this store, at its size.

    A database restored out of step with the files, or a file removed,
    truncated or edited by hand, mustn't leave a new item linked to a file
    that isn't what was recorded. (``verify --deep`` re-hashes; this is the
    cheap check.)
    """
    try:
        return store.exists(ref) and store.stat(ref).size == size
    except StoreError:  # another backend's reference
        return False


def _put_original(
    conn: psycopg.Connection[dict[str, Any]],
    store: EvidenceStore,
    data: bytes,
    *,
    media_type: str,
    folder: StoreFolder,
    name: str,
) -> StoredFile:
    """Store one original, or reuse the store's copy of the same bytes."""
    digest = hashlib.sha256(data).digest()
    existing = store_original_ref(conn, digest)
    if existing is not None and _present(store, existing, len(data)):
        return StoredFile(
            sha256=digest, byte_size=len(data), media_type=media_type, ref=existing
        )
    return store.put(
        data, media_type=media_type, purpose="original", folder=folder, name=name
    )


def store_email_original(
    conn: psycopg.Connection[dict[str, Any]],
    store: EvidenceStore,
    raw: RawReceipt,
    *,
    source_name: str,
    received_at: datetime,
) -> StoredEmail:
    """Store an email's ``message.eml`` and its attachments; record nothing.

    The folder is ``originals/YYYY/MM/{received-date}__{source}__{hash}``,
    from the ``.eml``'s sha256 and ``received_at``. An email with no Date
    header is dated when it was fetched, so a retry on a later day writes a
    second folder and leaves the first for ``verify --orphans``. Bytes that
    occur twice in the email are written once.

    ``conn`` is only read, to find bytes the store already holds; the caller
    owns its transaction. Raises ``ValueError`` without ``raw.raw_bytes``.
    """
    if raw.raw_bytes is None:
        msg = f"no raw bytes for {raw.source_id}"
        raise ValueError(msg)
    sha256 = hashlib.sha256(raw.raw_bytes).digest()
    folder = StoreFolder.for_original(received_at, source_name, sha256)
    # Within one email, the same bytes are stored once: nothing is recorded
    # until the item's transaction, so the database can't dedupe them yet.
    stored: dict[bytes, StoredFile] = {}

    def put(data: bytes, media_type: str, name: str) -> StoredFile:
        digest = hashlib.sha256(data).digest()
        if digest not in stored:
            stored[digest] = _put_original(
                conn, store, data, media_type=media_type, folder=folder, name=name
            )
        return stored[digest]

    message = put(raw.raw_bytes, MESSAGE_MEDIA_TYPE, MESSAGE_NAME)
    attachments = [
        StoredAttachment(
            file=put(
                att.data,
                base_media_type(att.content_type),
                attachment_name(position, att),
            ),
            file_name=att.mime_filename,
        )
        for position, att in enumerate(raw.attachments, start=1)
    ]
    return StoredEmail(message=message, attachments=attachments)


def _record(conn: psycopg.Connection[dict[str, Any]], stored: StoredFile) -> StoredFile:
    if stored.sha256 is None or stored.byte_size is None:
        msg = f"original {stored.ref} has no hash"
        raise ValueError(msg)
    file_id = record_original(
        conn,
        ref=stored.ref,
        sha256=stored.sha256,
        byte_size=stored.byte_size,
        media_type=stored.media_type,
    )
    return replace(stored, id=file_id)


def record_email_original(
    conn: psycopg.Connection[dict[str, Any]], email: StoredEmail
) -> StoredEmail:
    """Record a stored email's files and locations. Doesn't commit.

    Returns the same email with every file's ``id`` set.
    """
    return StoredEmail(
        message=_record(conn, email.message),
        attachments=[
            StoredAttachment(file=_record(conn, att.file), file_name=att.file_name)
            for att in email.attachments
        ],
    )


def _recordable_name(name: str | None) -> str | None:
    """A raw MIME file name as ``provenance_files.file_name`` can hold it.

    PostgreSQL text can't hold NUL, which an RFC 2231 name can encode; it
    becomes U+FFFD. Every other character is kept as sent.
    """
    return None if name is None else name.replace("\x00", "\ufffd")


@dataclass(frozen=True)
class EmailEvidence:
    """What a provenance row records about an email's stored original."""

    original_id: UUID
    document_id: UUID | None
    certainty: DocumentCertainty | None
    # (stored file ID, role, raw file name), message first: provenance_files.
    files: list[tuple[UUID, ProvenanceFileRole, str | None]]


def record_email_evidence(
    conn: psycopg.Connection[dict[str, Any]],
    email: StoredEmail,
    *,
    receipt_document: str | None,
    attachments: Sequence[Attachment],
) -> EmailEvidence:
    """Record an email's stored files and pick its receipt document.

    Doesn't commit. ``attachments`` are the parsed attachments the email was
    stored from, in the same order; ``receipt_document`` is the extraction's.
    """
    recorded = record_email_original(conn, email)
    message_id = recorded.message.id
    attachment_ids = [att.file.id for att in recorded.attachments]
    if message_id is None or None in attachment_ids:
        msg = "record_email_original left a file unrecorded"
        raise AssertionError(msg)
    ids = [i for i in attachment_ids if i is not None]
    files: list[tuple[UUID, ProvenanceFileRole, str | None]] = [
        (message_id, "message", None),
        *(
            (file_id, "attachment", _recordable_name(att.file_name))
            for file_id, att in zip(ids, recorded.attachments, strict=True)
        ),
    ]
    index, certainty = select_receipt_document(receipt_document, attachments)
    return EmailEvidence(
        original_id=message_id,
        document_id=None if index is None else ids[index],
        certainty=certainty,
        files=files,
    )
