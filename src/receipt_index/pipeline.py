"""Ingest pipeline orchestration.

``process_item`` runs one fetched item through extract, render, store and
index. ``handle_item`` wraps it in the per-item protocol: item lock, re-check,
``started`` row, fetch. ``run_ingest`` is the one-shot loop over one source:
it reads what is new since the stored sync state and advances the cursor
under the cursor rule (see the always-on ingest design, sections 1 and 5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.extraction import EXTRACTION_PROMPT_VERSION, extract_metadata
from receipt_index.locks import SourceLockLostError, item_lock
from receipt_index.models import MIN_RECEIPT_CONFIDENCE, receipt_values
from receipt_index.renderer import render_pdf
from receipt_index.repository import (
    get_processed_source_ids,
    get_stale_started,
    get_sync_state,
    insert_extraction,
    insert_ingest_log,
    insert_receipt,
    is_source_id_known,
    record_poll,
    save_drive_cursor,
    save_imap_cursor,
)
from receipt_index.store import pdf_path_for, store_rendition

if TYPE_CHECKING:
    import psycopg
    from pydantic_ai import Agent

    from receipt_index.adapters.base import SourceAdapter
    from receipt_index.locks import SourceLock
    from receipt_index.models import (
        ExtractionResult,
        ItemRef,
        RawReceipt,
        Receipt,
        ReceiptMetadata,
        SourceRead,
        SyncState,
    )
    from receipt_index.store import EvidenceStore

logger = logging.getLogger(__name__)

_MIN_CONFIDENCE = MIN_RECEIPT_CONFIDENCE

# A started row older than this, with no later row, belongs to a process that
# died mid-item.
_STALE_STARTED_SECONDS = 3600

ItemStatus = Literal["success", "skipped", "failed", "known", "locked"]


@dataclass
class IngestResult:
    """Summary of an ingest run over one source."""

    processed: int = 0
    skipped: int = 0
    failed: int = 0
    receipts: list[Receipt] = field(default_factory=list)
    # Set when the source itself failed: it couldn't be read, or its lock
    # was lost. The CLI exits non-zero.
    source_error: str | None = None


@dataclass
class ItemOutcome:
    """What happened to one item."""

    status: ItemStatus
    receipt: Receipt | None = None


def _email_date(source_type: str, ref_or_raw: ItemRef | RawReceipt) -> Any:
    return ref_or_raw.date if source_type == "imap" else None


def _record_failure(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    source_id: str,
    source_type: str,
    source_name: str,
    subject: str,
    sender: str,
    email_date: Any,
    exc: Exception,
    extraction: ExtractionResult | None,
    llm_model: str | None,
) -> None:
    """Write an item's ``failed`` row. A failure to write it is only logged."""
    try:
        insert_ingest_log(
            conn,
            source_id=source_id,
            source_type=source_type,
            source_name=source_name,
            status="failed",
            email_subject=subject,
            email_sender=sender,
            email_date=email_date,
            error_message=str(exc),
            llm_input_tokens=(extraction.input_tokens if extraction else None),
            llm_output_tokens=(extraction.output_tokens if extraction else None),
            llm_cache_read_tokens=(
                extraction.cache_read_tokens if extraction else None
            ),
            llm_requests=(extraction.requests if extraction else None),
            llm_model=llm_model,
        )
    except Exception:
        logger.warning("Failed to write ingest log for %s", source_id, exc_info=True)


def process_item(
    conn: psycopg.Connection[dict[str, Any]],
    raw: RawReceipt,
    *,
    store: EvidenceStore,
    source_name: str,
    source_type: str,
    llm_model: str,
    agent: Agent[None, ReceiptMetadata] | None = None,
) -> ItemOutcome:
    """Extract, render, store and index one fetched item.

    Writes the item's outcome row: ``success``, ``skipped`` (confidence below
    the threshold) or ``failed``. An error never propagates.
    """
    extraction = None
    try:
        extraction = extract_metadata(raw, agent=agent)
        metadata = extraction.metadata

        # Skip non-receipts based on low LLM confidence only.
        # amount == 0 is valid (e.g. prepaid postage receipts).
        if metadata.confidence < _MIN_CONFIDENCE:
            reason = f"amount={metadata.amount}, confidence={metadata.confidence:.2f}"
            logger.info(
                "Skipped non-receipt %s (%s): %s",
                raw.source_id,
                raw.subject,
                reason,
            )
            insert_ingest_log(
                conn,
                source_id=raw.source_id,
                source_type=source_type,
                source_name=source_name,
                status="skipped",
                vendor=metadata.vendor,
                amount=metadata.amount,
                paid_amount=metadata.paid_amount,
                email_subject=raw.subject,
                email_sender=raw.sender,
                email_date=_email_date(raw.source_type, raw),
                error_message=f"Skipped: {reason}",
                llm_input_tokens=extraction.input_tokens,
                llm_output_tokens=extraction.output_tokens,
                llm_cache_read_tokens=extraction.cache_read_tokens,
                llm_requests=extraction.requests,
                llm_model=llm_model,
            )
            return ItemOutcome("skipped")

        pdf_data = render_pdf(raw)
        stored = store_rendition(
            store,
            pdf_data,
            receipt_date=metadata.date,
            vendor=metadata.vendor,
            amount=metadata.amount,
        )
        receipt = insert_receipt(
            conn,
            source_id=raw.source_id,
            source_type=source_type,
            source_name=source_name,
            vendor=metadata.vendor,
            amount=metadata.amount,
            paid_amount=metadata.paid_amount,
            currency=metadata.currency,
            receipt_date=metadata.date,
            description=metadata.description,
            confidence=metadata.confidence,
            field_confidence=metadata.field_confidence.model_dump(exclude_none=True),
            pdf_path=pdf_path_for(stored.ref),
            email_subject=raw.subject,
            email_sender=raw.sender,
            email_date=_email_date(raw.source_type, raw),
            file_name=raw.file_name,
            commit=False,
        )
        # The receipt, its extraction row and the success row commit together.
        insert_extraction(
            conn,
            receipt_id=receipt.id,
            origin="ingest",
            input_kind="source",
            prompt_version=EXTRACTION_PROMPT_VERSION,
            llm_model=llm_model,
            output=metadata.model_dump(mode="json"),
            applied_fields=[
                name
                for name, value in receipt_values(metadata).items()
                if value is not None
            ],
            replaced={},
            llm_input_tokens=extraction.input_tokens,
            llm_output_tokens=extraction.output_tokens,
            llm_cache_read_tokens=extraction.cache_read_tokens,
            llm_requests=extraction.requests,
        )
        insert_ingest_log(
            conn,
            source_id=raw.source_id,
            source_type=source_type,
            source_name=source_name,
            status="success",
            receipt_id=receipt.id,
            vendor=metadata.vendor,
            amount=metadata.amount,
            paid_amount=metadata.paid_amount,
            email_subject=raw.subject,
            email_sender=raw.sender,
            email_date=_email_date(raw.source_type, raw),
            llm_input_tokens=extraction.input_tokens,
            llm_output_tokens=extraction.output_tokens,
            llm_cache_read_tokens=extraction.cache_read_tokens,
            llm_requests=extraction.requests,
            llm_model=llm_model,
        )
        logger.info(
            "Processed receipt: %s (%s) confidence=%.2f",
            metadata.vendor,
            receipt.id,
            metadata.confidence,
        )
        return ItemOutcome("success", receipt)
    except Exception as exc:
        # Rollback failed transaction so the ingest_log write succeeds
        conn.rollback()
        _record_failure(
            conn,
            source_id=raw.source_id,
            source_type=source_type,
            source_name=source_name,
            subject=raw.subject,
            sender=raw.sender,
            email_date=_email_date(raw.source_type, raw),
            exc=exc,
            extraction=extraction,
            llm_model=llm_model,
        )
        logger.warning(
            "Failed to process message %s (subject=%s, sender=%s)",
            raw.source_id,
            raw.subject,
            raw.sender,
            exc_info=True,
        )
        return ItemOutcome("failed")


def handle_item(
    conn: psycopg.Connection[dict[str, Any]],
    adapter: SourceAdapter,
    ref: ItemRef,
    *,
    store: EvidenceStore,
    source_name: str,
    source_type: str,
    llm_model: str,
    agent: Agent[None, ReceiptMetadata] | None = None,
) -> ItemOutcome:
    """Take the item lock, re-check, write ``started``, fetch and process.

    Returns ``locked`` when another process holds the item, and ``known``
    when the item gained a record between listing and locking. Once the
    ``started`` row is committed, every error path ends in an outcome row.

    The body is fetched before the ``started`` row is written, so a source
    that can't be reached (``SourceUnavailableError``, re-raised to the caller)
    leaves no row at all: the cursor stays below the item and the next run
    tries it again, as it would after a crash at the same point.
    """
    with item_lock(conn, ref.source_id) as locked:
        if not locked:
            return ItemOutcome("locked")
        if is_source_id_known(conn, ref.source_id):
            return ItemOutcome("known")
        # End the re-check's read transaction before the download, so the
        # connection never sits idle in a transaction while a fetch waits.
        conn.commit()

        email_date = _email_date(ref.source_type, ref)
        if ref.skip_reason is not None:
            mime = (ref.drive_meta or {}).get("mimeType", "unknown")
            insert_ingest_log(
                conn,
                source_id=ref.source_id,
                source_type=source_type,
                source_name=source_name,
                status="skipped",
                email_subject=ref.subject,
                email_sender=ref.sender,
                email_date=email_date,
                error_message=(
                    f"Skipped: {ref.skip_reason} ({mime}, {ref.file_name or '?'})"
                ),
            )
            logger.info("Skipped %s: %s (%s)", ref.source_id, ref.skip_reason, mime)
            return ItemOutcome("skipped")

        fetch_error: Exception | None = None
        raw: RawReceipt | None = None
        try:
            raw = adapter.fetch(ref)
            if raw is None:
                fetch_error = LookupError("item is no longer in the source")
        except SourceUnavailableError:
            raise
        except Exception as err:  # the item itself is at fault (e.g. parse)
            fetch_error = err

        # Committed before any processing, so a crash leaves a trace and the
        # cursor may pass the item.
        insert_ingest_log(
            conn,
            source_id=ref.source_id,
            source_type=source_type,
            source_name=source_name,
            status="started",
            email_subject=ref.subject,
            email_sender=ref.sender,
            email_date=email_date,
        )

        if fetch_error is not None or raw is None:
            _record_failure(
                conn,
                source_id=ref.source_id,
                source_type=source_type,
                source_name=source_name,
                subject=ref.subject,
                sender=ref.sender,
                email_date=email_date,
                exc=fetch_error or LookupError("item is no longer in the source"),
                extraction=None,
                llm_model=llm_model,
            )
            logger.warning("Failed to fetch %s", ref.source_id, exc_info=fetch_error)
            return ItemOutcome("failed")

        return process_item(
            conn,
            raw,
            store=store,
            source_name=source_name,
            source_type=source_type,
            agent=agent,
            llm_model=llm_model,
        )


class _Cursor:
    """Advances one source's stored position under the cursor rule.

    The cursor only moves to a position at or below which every item has a
    committed record (a ``started`` row, an outcome, or an earlier record).

    - IMAP: it moves over the UIDs the search returned, in order, and stops
      at the first one not yet handled (locked, cut off by ``--limit``, or
      with no ref at all). ``uidvalidity`` and ``last_uid`` are written
      together.
    - Drive: a page token covers a whole feed position, so the new token is
      saved only when every item was handled and nothing was held back.
    """

    def __init__(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        read: SourceRead,
        state: SyncState | None,
        *,
        source_name: str,
        source_type: str,
        folder: str,
        source_lock: SourceLock | None,
    ) -> None:
        self.conn = conn
        self.read = read
        self.state = state
        self.source_name = source_name
        self.source_type = source_type
        self.folder = folder
        self.source_lock = source_lock
        self._done: set[Any] = set()
        self._index = 0
        self._pos = read.start_uid
        self._saved_pos: int | None = None

    def _check_lock(self) -> None:
        if self.source_lock is not None:
            self.source_lock.check()

    def begin(self) -> None:
        """Save the new IMAP numbering at once after a reset or a first run."""
        if self.source_type != "imap" or self.read.uidvalidity is None:
            return
        stored = self.state.imap_uidvalidity if self.state else None
        if stored != self.read.uidvalidity:
            self._save_imap(self.read.start_uid, reset=self.read.reset is not None)

    def mark_done(self, ref: ItemRef) -> None:
        """Record that ``ref`` has a committed record, and advance if possible."""
        if self.source_type == "imap":
            self._done.add(ref.imap_uid)
            uids = self.read.searched_uids
            while self._index < len(uids) and uids[self._index] in self._done:
                self._pos = uids[self._index]
                self._index += 1
            saved = self.read.start_uid if self._saved_pos is None else self._saved_pos
            if self._pos != saved:
                self._save_imap(self._pos)
        else:
            self._done.add(ref.source_id)

    def finish(self) -> None:
        """Save the Drive token if the whole read was handled."""
        if self.source_type == "imap":
            return
        token = self.read.drive_page_token
        account = self.read.drive_account_id
        if token is None or account is None:
            return
        handled = all(ref.source_id in self._done for ref in self.read.items)
        if not handled or self.read.held_back:
            logger.info(
                "Drive source %s: not advancing the page token "
                "(items left for a later run)",
                self.source_name,
            )
            return
        self._check_lock()
        save_drive_cursor(
            self.conn,
            source_name=self.source_name,
            folder=self.folder,
            page_token=token,
            account_id=account,
            reset=self.read.reset is not None,
        )

    def _save_imap(self, last_uid: int, *, reset: bool = False) -> None:
        assert self.read.uidvalidity is not None
        self._check_lock()
        save_imap_cursor(
            self.conn,
            source_name=self.source_name,
            folder=self.folder,
            uidvalidity=self.read.uidvalidity,
            last_uid=last_uid,
            reset=reset,
        )
        self._saved_pos = last_uid


def run_ingest(
    *,
    conn: psycopg.Connection[dict[str, Any]],
    adapter: SourceAdapter,
    store: EvidenceStore,
    source_name: str,
    source_type: str,
    llm_model: str,
    agent: Agent[None, ReceiptMetadata] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    source_lock: SourceLock | None = None,
) -> IngestResult:
    """Read what is new in one source and process it, one item at a time.

    The caller holds ``source_lock`` (the CLI does); it is checked before
    every cursor write. ``dry_run`` reads the new range but writes nothing:
    no rows, no locks, no cursor. ``limit`` caps the unseen items handled.
    """
    result = IngestResult()
    with adapter:
        folder = adapter.folder
        state = get_sync_state(conn, source_name, folder)
        try:
            read = adapter.list_new(state)
        except Exception as exc:
            logger.exception("Failed to read source %s", source_name)
            result.source_error = f"read failed: {exc}"
            if not dry_run:
                record_poll(
                    conn,
                    source_name=source_name,
                    source_type=source_type,
                    folder=folder,
                    error=str(exc),
                )
            return result
        if not dry_run:
            record_poll(
                conn,
                source_name=source_name,
                source_type=source_type,
                folder=folder,
                error=None,
            )
        if read.reset:
            logger.warning("Source %s: cursor reset (%s)", source_name, read.reset)

        # Check all source_ids globally: the UNIQUE constraint is on source_id
        # alone, so skip any ID that exists regardless of source_name.
        processed_ids = get_processed_source_ids(conn)
        cursor = _Cursor(
            conn,
            read,
            state,
            source_name=source_name,
            source_type=source_type,
            folder=folder,
            source_lock=source_lock,
        )

        try:
            if not dry_run:
                cursor.begin()
            attempted = 0
            for ref in read.items:
                if ref.source_id in processed_ids:
                    logger.debug("Skipping already-processed item %s", ref.source_id)
                    if not dry_run:
                        cursor.mark_done(ref)
                    continue
                if limit is not None and attempted >= limit:
                    break
                attempted += 1

                if dry_run:
                    logger.info("Dry run: would process %s", ref.source_id)
                    result.skipped += 1
                    continue

                outcome = handle_item(
                    conn,
                    adapter,
                    ref,
                    store=store,
                    source_name=source_name,
                    source_type=source_type,
                    agent=agent,
                    llm_model=llm_model,
                )
                if outcome.status == "locked":
                    logger.info(
                        "Item %s is locked by another process; leaving it",
                        ref.source_id,
                    )
                    continue
                processed_ids.add(ref.source_id)
                if outcome.status == "success" and outcome.receipt is not None:
                    result.processed += 1
                    result.receipts.append(outcome.receipt)
                elif outcome.status == "skipped":
                    result.skipped += 1
                elif outcome.status == "failed":
                    result.failed += 1
                cursor.mark_done(ref)

            if not dry_run:
                cursor.finish()
        except (SourceLockLostError, SourceUnavailableError) as exc:
            logger.error("Source %s: %s; stopping this source", source_name, exc)
            result.source_error = str(exc)
            record_poll(
                conn,
                source_name=source_name,
                source_type=source_type,
                folder=folder,
                error=str(exc),
            )

    return result


def warn_stale_started(conn: psycopg.Connection[dict[str, Any]]) -> list[str]:
    """Log items left with only an old ``started`` row, and return their IDs.

    Their process died mid-item. They count as processed, so nothing retries
    them until the retry scheduler exists; this makes them visible.
    """
    rows = get_stale_started(conn, older_than_seconds=_STALE_STARTED_SECONDS)
    ids = [str(row["source_id"]) for row in rows]
    if ids:
        logger.warning(
            "%d item(s) have a 'started' row older than an hour and no outcome "
            "(their run stopped mid-item; they aren't retried automatically): %s",
            len(ids),
            ", ".join(ids),
        )
    return ids
