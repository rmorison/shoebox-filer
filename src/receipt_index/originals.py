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
from receipt_index.store import StoredFile, StoreFolder

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    import psycopg

    from receipt_index.models import Attachment, RawReceipt
    from receipt_index.repository import DocumentCertainty
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
    if existing is not None:
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
    second folder and leaves the first for ``verify --orphans``. ``conn`` is
    only read, to find bytes the store already holds. Raises ``ValueError``
    without ``raw.raw_bytes``.
    """
    if raw.raw_bytes is None:
        msg = f"no raw bytes for {raw.source_id}"
        raise ValueError(msg)
    sha256 = hashlib.sha256(raw.raw_bytes).digest()
    folder = StoreFolder.for_original(received_at, source_name, sha256)
    message = _put_original(
        conn,
        store,
        raw.raw_bytes,
        media_type=MESSAGE_MEDIA_TYPE,
        folder=folder,
        name=MESSAGE_NAME,
    )
    attachments = [
        StoredAttachment(
            file=_put_original(
                conn,
                store,
                att.data,
                media_type=base_media_type(att.content_type),
                folder=folder,
                name=attachment_name(position, att),
            ),
            file_name=att.mime_filename,
        )
        for position, att in enumerate(raw.attachments, start=1)
    ]
    # End the lookups' read transaction before the item goes on to render.
    conn.commit()
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
