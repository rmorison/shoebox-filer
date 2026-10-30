"""Record the renditions stored before the evidence-store schema.

``store backfill-hashes`` hashes each receipt's local rendition and records
its ``stored_files`` row, its own ``local`` location and
``receipts.rendition_file_id`` (evidence-store design, Implementation Plan
ticket 2). It selects only receipts with no ``rendition_file_id``, so a
second run changes nothing and a run cut short resumes where it stopped.
Each receipt commits on its own.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from receipt_index.repository import (
    RenditionConflictError,
    receipts_without_rendition,
    record_rendition,
)
from receipt_index.store import InvalidRefError, rendition_ref

if TYPE_CHECKING:
    from uuid import UUID

    import psycopg

    from receipt_index.store import EvidenceStore

logger = logging.getLogger(__name__)

_PDF_MEDIA_TYPE = "application/pdf"


@dataclass
class BackfillReport:
    """What a backfill run did, receipt by receipt."""

    hashed: int = 0
    shared: int = 0  # recorded against a stored file another receipt has
    missing: list[tuple[UUID, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def backfill_rendition_hashes(
    conn: psycopg.Connection[dict[str, Any]],
    store: EvidenceStore,
    *,
    dry_run: bool = False,
) -> BackfillReport:
    """Hash and record every receipt's rendition that isn't recorded yet.

    A receipt whose file is missing, unreadable or already recorded for
    another receipt is reported in ``missing`` with the reason and left
    alone, so a later run picks it up. With ``dry_run`` the files are read
    and hashed, and nothing is written.
    """
    report = BackfillReport()
    seen: set[bytes] = set()
    for receipt_id, pdf_path in receipts_without_rendition(conn):
        conn.rollback()  # end the read's transaction; one per receipt below
        if pdf_path is None:
            report.missing.append((receipt_id, "no pdf_path"))
            continue
        ref = rendition_ref(pdf_path)
        try:
            with store.open(ref) as handle:
                data = handle.read()
        except (OSError, InvalidRefError) as exc:
            report.missing.append((receipt_id, f"{ref}: {type(exc).__name__}"))
            continue
        digest = hashlib.sha256(data).digest()
        if dry_run:
            report.shared += digest in seen
            seen.add(digest)
            report.hashed += 1
            continue
        known = conn.execute(
            "SELECT 1 FROM receipt.stored_files WHERE sha256 = %(sha256)s",
            {"sha256": digest},
        ).fetchone()
        try:
            record_rendition(
                conn,
                receipt_id=receipt_id,
                ref=ref,
                sha256=digest,
                byte_size=len(data),
                media_type=_PDF_MEDIA_TYPE,
            )
        except RenditionConflictError as exc:
            conn.rollback()
            report.missing.append((receipt_id, str(exc)))
            continue
        conn.commit()
        report.hashed += 1
        report.shared += known is not None
    conn.rollback()
    return report
