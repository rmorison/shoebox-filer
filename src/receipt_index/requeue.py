"""``retry``: requeue failed or skipped items without editing the log.

Each requeued item gets a ``queued`` row that points at one
``retry_requests`` row (who asked, when, with which filters, how many items
matched and how many were requeued). The next ``ingest`` runs queued items
through the retry scheduler, as attempt 1.
"""

from __future__ import annotations

import getpass
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from receipt_index.locks import item_lock
from receipt_index.repository import (
    get_item_history,
    has_receipt,
    insert_ingest_log,
    insert_retry_request,
    orphaned_refs,
    set_retry_request_count,
)

if TYPE_CHECKING:
    from uuid import UUID

    import psycopg

    from receipt_index.models import IngestLogEntry
    from receipt_index.repository import RetryFilter
    from receipt_index.store import EvidenceStore

logger = logging.getLogger(__name__)


@dataclass
class OrphanReport:
    """Stored renditions left by failures of the matched items.

    ``files`` maps a source ID to renditions a failed row recorded, that
    still exist and that no receipt references. ``unrecorded`` lists matched
    items whose failure was written before stored files were recorded, so
    the report can't tell; check those by hand.
    """

    files: dict[str, list[str]] = field(default_factory=dict)
    unrecorded: list[str] = field(default_factory=list)


@dataclass
class RequeueOutcome:
    """What ``retry --yes`` did."""

    request_id: UUID
    requeued: list[str] = field(default_factory=list)
    # (source ID, reason) for items left alone.
    not_requeued: list[tuple[str, str]] = field(default_factory=list)


def orphan_report(
    conn: psycopg.Connection[dict[str, Any]],
    store: EvidenceStore,
    candidates: list[IngestLogEntry],
) -> OrphanReport:
    """Report orphaned renditions for the matched items. Never deletes."""
    ids = [c.source_id for c in candidates]
    report = OrphanReport()
    for source_id, refs in orphaned_refs(conn, ids).items():
        present = [ref for ref in refs if _exists(store, ref)]
        if present:
            report.files[source_id] = present
    # A row without a class was written before failures were classified,
    # and so before stored files were recorded.
    report.unrecorded = [
        c.source_id
        for c in candidates
        if c.status == "failed" and c.error_class is None
    ]
    return report


def _exists(store: EvidenceStore, ref: str) -> bool:
    try:
        return store.exists(ref)
    except Exception:
        logger.warning("Can't check stored file %s", ref, exc_info=True)
        return False


def _requested_by() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def apply_requeue(
    conn: psycopg.Connection[dict[str, Any]],
    spec: RetryFilter,
    candidates: list[IngestLogEntry],
) -> RequeueOutcome:
    """Requeue each candidate under its item lock.

    The request is recorded first, since each ``queued`` row points at it;
    its ``requeued`` count is written once the loop is done. An item whose
    lock is held, or whose latest row is no longer the one listed, is left
    alone and reported.
    """
    request_id = insert_retry_request(
        conn,
        requested_by=_requested_by(),
        filters=spec.to_json(),
        matched=len(candidates),
    )
    outcome = RequeueOutcome(request_id)
    try:
        for entry in candidates:
            with item_lock(conn, entry.source_id) as locked:
                if not locked:
                    outcome.not_requeued.append(
                        (entry.source_id, "another process holds it")
                    )
                    continue
                history = get_item_history(conn, entry.source_id, limit=1)
                if (
                    not history
                    or history[0].id != entry.id
                    or has_receipt(conn, entry.source_id)
                ):
                    conn.commit()
                    outcome.not_requeued.append(
                        (entry.source_id, "its state changed since it was listed")
                    )
                    continue
                insert_ingest_log(
                    conn,
                    source_id=entry.source_id,
                    source_type=entry.source_type,
                    source_name=entry.source_name,
                    status="queued",
                    email_subject=entry.email_subject,
                    email_sender=entry.email_sender,
                    email_date=entry.email_date,
                    error_message=f"Requeued by retry request {request_id}",
                    attempt=0,
                    counts_attempt=False,
                    retry_request_id=request_id,
                )
                outcome.requeued.append(entry.source_id)
    finally:
        # Every queued row so far is committed; a write that failed mid-loop
        # left an aborted transaction, which the count update can't use.
        conn.rollback()
        set_retry_request_count(conn, request_id, len(outcome.requeued))
    return outcome
