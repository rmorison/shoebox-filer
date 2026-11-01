"""Backfill: process a source's back catalogue over a date range.

Two modes (always-on ingest design, section 8):

- ``run_backfill``, the full mode, lists one source over a date range and
  runs every item it hasn't seen through ``handle_item``. For a seen IMAP
  item whose original is still ``pending``, it stores the original.
- ``run_originals_only`` re-fetches the original of each receipt that has
  none, through the shared fetch path, and stores it with no extraction. An
  item that is gone is marked ``unavailable`` with a reason.

Both take item locks, never the source lock, and never write sync state, so
they run safely beside ``serve``. A re-run over the same range does nothing
new.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.adapters.imap import (
    AmbiguousMatchError,
    can_search_by_id,
    lookup_folders,
)
from receipt_index.config import ImapSourceConfig
from receipt_index.fetching import SourceDownError, fetch_item
from receipt_index.locks import item_lock
from receipt_index.originals import attach_email_original, store_email_original
from receipt_index.pipeline import handle_item
from receipt_index.repository import (
    get_processed_source_ids,
    get_provenance,
    latest_receipt_document,
    mark_original_unavailable,
    provenance_missing_originals,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    import psycopg
    from pydantic_ai import Agent

    from receipt_index.adapters.base import SourceAdapter
    from receipt_index.fetching import SourcePool
    from receipt_index.models import (
        Provenance,
        RawReceipt,
        Receipt,
        ReceiptMetadata,
    )
    from receipt_index.retries import RetryPolicy
    from receipt_index.store import EvidenceStore

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    """Summary of a full-mode backfill over one source.

    ``unseen`` counts the items handed to ``handle_item`` (in a dry run, the
    items that would be). ``originals_missing`` counts the pending
    provenance rows of seen items that were (or would be) filled in; both
    count towards ``limit``.
    """

    listed: int = 0
    unseen: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    locked: int = 0
    originals_missing: int = 0
    originals_stored: int = 0
    originals_failed: int = 0
    receipts: list[Receipt] = field(default_factory=list)
    source_error: str | None = None


@dataclass
class OriginalsResult:
    """Summary of a ``--originals-only`` run over one source.

    ``to_fetch`` counts the rows looked up (in a dry run, those that would
    be). ``unsearchable`` rows have an ID the shared fetch path can't search
    for, ``ambiguous`` rows share their exact Message-ID with another
    message, and ``drive_pending`` rows wait for the Drive backend: all stay
    pending. Only a search that found no message marks a row unavailable.
    """

    to_fetch: int = 0
    stored: int = 0
    unavailable: int = 0
    failed: int = 0
    ambiguous: int = 0
    locked: int = 0
    unchanged: int = 0
    unsearchable: int = 0
    drive_pending: int = 0
    source_error: str | None = None


def _attach(
    conn: psycopg.Connection[dict[str, Any]],
    store: EvidenceStore,
    provenance: Provenance,
    raw: RawReceipt,
) -> bool:
    """Store a fetched email's original and link it to its provenance row.

    Dated by the row's ``received_at``, so a re-run lands in the same
    content-addressed folder. One transaction for the rows; the caller
    holds the item lock.
    """
    email = store_email_original(
        conn,
        store,
        raw,
        source_name=provenance.source_name,
        received_at=provenance.received_at,
    )
    document = latest_receipt_document(conn, provenance.receipt_id)
    attached = attach_email_original(
        conn,
        provenance.id,
        email,
        attachments=raw.attachments,
        receipt_document=document,
    )
    if attached:
        conn.commit()
    else:
        conn.rollback()
    return attached


def _fill_original(
    conn: psycopg.Connection[dict[str, Any]],
    store: EvidenceStore,
    provenance: Provenance,
    fetch: Callable[[], RawReceipt | None],
    *,
    unavailable_reason: str | None = None,
) -> str:
    """Lock, re-check, fetch and attach one pending original.

    Returns ``stored``, ``locked``, ``unchanged`` (no longer pending),
    ``ambiguous`` (several messages carry the ID: left pending),
    ``failed`` (``fetch`` raised, or storing failed: the row stays pending),
    and when ``fetch`` found nothing, ``unavailable`` (marked with
    ``unavailable_reason``) or, without a reason, ``gone`` (left pending).
    ``SourceUnavailableError`` and ``SourceDownError`` propagate: the source
    is down, and nothing is marked.
    """
    with item_lock(conn, provenance.source_id) as locked:
        if not locked:
            return "locked"
        current = get_provenance(conn, provenance.id)
        conn.commit()
        if current is None or current.original_status != "pending":
            return "unchanged"
        try:
            raw = fetch()
        except (SourceUnavailableError, SourceDownError):
            raise
        except AmbiguousMatchError as exc:
            logger.warning(
                "Original of %s left pending: %s (resolve by hand)",
                current.source_id,
                exc,
            )
            return "ambiguous"
        except Exception:
            logger.warning(
                "Can't fetch the original of %s", current.source_id, exc_info=True
            )
            return "failed"
        if raw is None:
            if unavailable_reason is None:
                return "gone"
            marked = mark_original_unavailable(conn, current.id, unavailable_reason)
            conn.commit()
            return "unavailable" if marked else "unchanged"
        try:
            return "stored" if _attach(conn, store, current, raw) else "unchanged"
        except Exception:
            conn.rollback()
            logger.warning(
                "Can't store the original of %s", current.source_id, exc_info=True
            )
            return "failed"


def _lookup(pool: SourcePool, provenance: Provenance) -> RawReceipt | None:
    """Find a row's item through the shared fetch path.

    Anything but a completed search with no match raises (a refusal, an
    empty response, several matches): none of those proves the item is
    gone, so the row is never marked unavailable for it.
    """
    fetched = fetch_item(
        pool,
        source_id=provenance.source_id,
        source_name=provenance.source_name,
        source_type=provenance.source_type,
    )
    if fetched.raw is None and fetched.item_error is not None:
        raise fetched.item_error
    return fetched.raw


def _once(fetch: Callable[[], RawReceipt | None]) -> Callable[[], RawReceipt | None]:
    """``fetch``, called at most once successfully; later calls reuse it."""
    got: list[RawReceipt | None] = []

    def call() -> RawReceipt | None:
        if not got:
            got.append(fetch())
        return got[0]

    return call


def run_backfill(
    conn: psycopg.Connection[dict[str, Any]],
    adapter: SourceAdapter,
    *,
    store: EvidenceStore,
    source_name: str,
    source_type: str,
    llm_model: str,
    agent: Agent[None, ReceiptMetadata] | None,
    since: date | None = None,
    until: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    policy: RetryPolicy | None = None,
) -> BackfillResult:
    """Process the items of one source, over a date range, that it hasn't seen.

    Unseen means no provenance row and no ``ingest_log`` row, the rule
    every full scan uses: a failed, skipped or retrying item stays the retry
    machinery's. ``limit`` caps the downloads: unseen items handled plus
    original fills. Every pending provenance row of a seen item is filled
    from one fetch. A dry run lists
    and counts, and writes nothing. No cursor moves and no source lock is
    taken.
    """
    result = BackfillResult()
    with adapter:
        try:
            refs = adapter.list_range(since, until)
        except Exception as exc:
            logger.exception("Failed to list source %s", source_name)
            result.source_error = f"listing failed: {exc}"
            return result
        result.listed = len(refs)
        processed_ids = get_processed_source_ids(conn)
        pending: dict[str, list[Provenance]] = {}
        if source_type == "imap":
            for row in provenance_missing_originals(conn, source_name):
                pending.setdefault(row.source_id, []).append(row)
        conn.commit()

        def spent() -> int:
            return result.unseen + result.originals_missing

        try:
            for ref in refs:
                if ref.source_id in processed_ids:
                    fetch = _once(partial(adapter.fetch, ref))
                    for provenance in pending.get(ref.source_id, []):
                        if limit is not None and spent() >= limit:
                            break
                        result.originals_missing += 1
                        if dry_run:
                            continue
                        status = _fill_original(conn, store, provenance, fetch)
                        if status == "stored":
                            result.originals_stored += 1
                        elif status in ("failed", "gone"):
                            result.originals_failed += 1
                    continue
                if limit is not None and spent() >= limit:
                    continue
                result.unseen += 1
                if dry_run:
                    logger.info("Dry run: would process %s", ref.source_id)
                    continue
                outcome = handle_item(
                    conn,
                    adapter,
                    ref,
                    store=store,
                    source_name=source_name,
                    source_type=source_type,
                    llm_model=llm_model,
                    agent=agent,
                    policy=policy,
                )
                if outcome.status == "locked":
                    logger.info("Item %s is locked; leaving it", ref.source_id)
                    result.locked += 1
                    continue
                processed_ids.add(ref.source_id)
                if outcome.status == "success" and outcome.receipt is not None:
                    result.processed += 1
                    result.receipts.append(outcome.receipt)
                elif outcome.status == "skipped":
                    result.skipped += 1
                elif outcome.status == "failed":
                    result.failed += 1
                if outcome.stops_source:
                    result.source_error = (
                        f"stopped after a {outcome.error_class} failure; the item "
                        "is retried on a later run"
                    )
                    logger.error("Source %s: %s", source_name, result.source_error)
                    break
        except SourceUnavailableError as exc:
            logger.error("Source %s: %s; stopping", source_name, exc)
            result.source_error = str(exc)
    return result


def run_originals_only(
    conn: psycopg.Connection[dict[str, Any]],
    pool: SourcePool,
    *,
    store: EvidenceStore,
    source_name: str,
    since: date | None = None,
    until: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> OriginalsResult:
    """Fetch and store the original of each of a source's receipts that has none.

    Makes no LLM call. Each pending row is looked up by its source ID
    through the shared fetch path. Found: stored and linked. Not found:
    marked ``unavailable`` with a reason. An item error leaves the row
    pending for a later run, and a source that can't be reached stops the
    run. Drive rows, and IDs that can't be searched for, stay pending.
    ``limit`` caps the rows looked up. The unavailable reason names the
    folders searched, so a later re-check can find those rows.
    """
    result = OriginalsResult()
    rows = provenance_missing_originals(conn, source_name, since, until)
    conn.commit()
    reason: str | None = None
    for provenance in rows:
        if provenance.source_type != "imap":
            result.drive_pending += 1
            continue
        if not can_search_by_id(provenance.source_id):
            result.unsearchable += 1
            continue
        if limit is not None and result.to_fetch >= limit:
            continue
        result.to_fetch += 1
        if dry_run:
            continue

        if reason is None:
            reason = _unavailable_reason(pool, source_name)
        try:
            status = _fill_original(
                conn,
                store,
                provenance,
                partial(_lookup, pool, provenance),
                unavailable_reason=reason,
            )
        except SourceDownError as exc:
            logger.error("Originals for %s stopped: %s", source_name, exc)
            result.source_error = str(exc)
            break
        if status == "unavailable":
            result.unavailable += 1
            logger.info("Original of %s is unavailable", provenance.source_id)
        elif status == "stored":
            result.stored += 1
        elif status == "failed":
            result.failed += 1
        elif status == "ambiguous":
            result.ambiguous += 1
        elif status == "locked":
            result.locked += 1
        else:
            result.unchanged += 1
    return result


def _unavailable_reason(pool: SourcePool, source_name: str) -> str:
    """Why a row is unavailable: which folders the search found nothing in.

    Read from the source's configuration, so it opens no connection: a
    source that is down is met inside the lookup, and stops the run there.
    """
    config = pool.config(source_name)
    folders = lookup_folders(config) if isinstance(config, ImapSourceConfig) else []
    where = ", ".join(folders) if folders else f"source {source_name}"
    return f"not found in {where} by Message-ID (source {source_name})"
