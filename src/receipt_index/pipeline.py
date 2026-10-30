"""Ingest pipeline orchestration.

``process_item`` runs one fetched item through extract, render, store and
index. ``handle_item`` wraps it in the per-item protocol: item lock, re-check,
fetch, ``started`` row. ``run_ingest`` is the one-shot loop over one source:
it reads what is new since the stored sync state and advances the cursor
under the cursor rule (see the always-on ingest design, sections 1 and 5).
``run_due_retries`` is the retry scheduler (section 6): it runs the items of
one source that are due again, through the shared re-fetch path.

Every failed row is classified (``receipt_index.errors``) and carries its
attempt and next retry time (``receipt_index.retries``). A transient or
config failure stops the source for the rest of the run: the next item would
most likely fail the same way.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.errors import classify
from receipt_index.extraction import EXTRACTION_PROMPT_VERSION, extract_metadata
from receipt_index.fetching import SourceDownError, fetch_item
from receipt_index.locks import SourceLockLostError, item_lock
from receipt_index.models import (
    MIN_RECEIPT_CONFIDENCE,
    link_host,
    receipt_values,
    redacted_traceback,
)
from receipt_index.refunds import link_by_rule
from receipt_index.renderer import render_pdf
from receipt_index.repository import (
    get_item_history,
    get_processed_source_ids,
    get_sync_state,
    has_receipt,
    insert_extraction,
    insert_ingest_log,
    insert_provenance,
    insert_receipt,
    is_due,
    is_source_id_known,
    orphaned_refs,
    record_poll,
    record_rendition,
    save_drive_cursor,
    save_imap_cursor,
)
from receipt_index.retries import (
    AttemptState,
    RetryPolicy,
    attempt_state,
    failure_fields,
)
from receipt_index.store import pdf_path_for, store_rendition

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import AbstractContextManager

    import psycopg
    from pydantic_ai import Agent

    from receipt_index.adapters.base import SourceAdapter
    from receipt_index.errors import ErrorClass
    from receipt_index.fetching import SourcePool
    from receipt_index.locks import SourceLock
    from receipt_index.models import (
        ExtractionResult,
        IngestLogEntry,
        ItemRef,
        RawReceipt,
        Receipt,
        ReceiptMetadata,
        SourceRead,
        SyncState,
    )
    from receipt_index.store import EvidenceStore, StoredFile

logger = logging.getLogger(__name__)

_MIN_CONFIDENCE = MIN_RECEIPT_CONFIDENCE

# The error a dead started row is closed with.
_DIED_MID_ITEM = "the process stopped mid-item (found by the retry scheduler)"

ItemStatus = Literal["success", "skipped", "failed", "known", "locked"]


class RunControl(Protocol):
    """How a long-running caller (``serve``) steers the item loops.

    ``should_stop`` is asked before each item: the loops stop between items,
    so no item is left half-done and the cursor rule holds. ``running`` wraps
    each item, so a watchdog can time it.
    """

    def should_stop(self) -> bool: ...

    def running(self, source_id: str) -> AbstractContextManager[object]: ...


def _running(
    control: RunControl | None, source_id: str
) -> AbstractContextManager[object]:
    return nullcontext() if control is None else control.running(source_id)


@dataclass
class IngestResult:
    """Summary of an ingest run over one source."""

    processed: int = 0
    skipped: int = 0
    failed: int = 0
    receipts: list[Receipt] = field(default_factory=list)
    # Set when the source itself failed: it couldn't be read, its lock was
    # lost, or an item failed in a way that stops the source (transient or
    # config). The CLI exits non-zero.
    source_error: str | None = None


@dataclass
class ItemOutcome:
    """What happened to one item."""

    status: ItemStatus
    receipt: Receipt | None = None
    error_class: ErrorClass | None = None

    @property
    def stops_source(self) -> bool:
        """A transient or config failure: the next item would fail too."""
        return self.status == "failed" and self.error_class in ("transient", "config")


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _stop_message(outcome: ItemOutcome) -> str:
    return (
        f"stopped after a {outcome.error_class} failure; the item is retried "
        "on a later run"
    )


def _email_date(source_type: str, ref_or_raw: ItemRef | RawReceipt) -> Any:
    return ref_or_raw.date if source_type == "imap" else None


def _record_failure(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    source_id: str,
    source_type: str,
    source_name: str | None,
    subject: str,
    sender: str,
    email_date: Any,
    exc: Exception,
    extraction: ExtractionResult | None,
    llm_model: str | None,
    policy: RetryPolicy,
    state: AttemptState,
    stored_ref: str | None = None,
    error_class: ErrorClass | None = None,
) -> ErrorClass:
    """Write an item's ``failed`` row and return its class.

    The class comes from the exception unless ``error_class`` is given. A
    failure to write the row is only logged.
    """
    cls = error_class or classify(exc)
    fields = failure_fields(policy, state, cls, _now())
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
            attempt=fields.attempt,
            counts_attempt=fields.counts_attempt,
            next_retry_at=fields.next_retry_at,
            error_class=fields.error_class,
            stored_ref=stored_ref,
        )
    except Exception:
        logger.warning("Failed to write ingest log for %s", source_id, exc_info=True)
    return cls


def record_ingest_evidence(
    conn: psycopg.Connection[dict[str, Any]],
    receipt: Receipt,
    raw: RawReceipt,
    rendition: StoredFile,
    *,
    source_name: str,
    source_type: str,
) -> None:
    """Write a new receipt's provenance row and its rendition's records."""
    insert_provenance(
        conn,
        receipt_id=receipt.id,
        source_type=source_type,
        source_name=source_name,
        source_id=raw.source_id,
        received_at=raw.date,
        email_subject=raw.subject,
        email_sender=raw.sender,
        email_date=_email_date(raw.source_type, raw),
        file_name=raw.file_name,
    )
    if rendition.sha256 is None or rendition.byte_size is None:
        msg = f"rendition {rendition.ref} has no hash"
        raise ValueError(msg)
    record_rendition(
        conn,
        receipt_id=receipt.id,
        ref=rendition.ref,
        sha256=rendition.sha256,
        byte_size=rendition.byte_size,
        media_type=rendition.media_type,
    )


def process_item(
    conn: psycopg.Connection[dict[str, Any]],
    raw: RawReceipt,
    *,
    store: EvidenceStore,
    source_name: str,
    source_type: str,
    llm_model: str,
    agent: Agent[None, ReceiptMetadata] | None = None,
    policy: RetryPolicy | None = None,
    state: AttemptState | None = None,
) -> ItemOutcome:
    """Extract, render, store and index one fetched item.

    Writes the item's outcome row: ``success``, ``skipped`` (confidence below
    the threshold) or ``failed``, numbered as attempt ``state.number``. An
    error never propagates.
    """
    policy = policy or RetryPolicy()
    state = state or AttemptState()
    extraction = None
    stored_ref: str | None = None
    try:
        extraction = extract_metadata(raw, agent=agent)
        metadata = extraction.metadata

        # Skip non-receipts based on low LLM confidence only.
        # amount == 0 is valid (e.g. prepaid postage receipts).
        if metadata.confidence < _MIN_CONFIDENCE:
            # A low-confidence item whose receipt is only behind a link is a
            # link-only skip: listed for review, never silently dropped. Only
            # the host is logged; the path and query can carry a personal
            # token, so the link itself goes on the row alone.
            link = metadata.receipt_link
            if link is not None:
                host = link_host(link)
                message = f"Skipped: link-only receipt ({host})"
                logger.info(
                    "Skipped link-only receipt %s (%s): host=%s",
                    raw.source_id,
                    raw.subject,
                    host,
                )
            else:
                reason = (
                    f"amount={metadata.amount}, confidence={metadata.confidence:.2f}"
                )
                message = f"Skipped: {reason}"
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
                error_message=message,
                receipt_link=link,
                llm_input_tokens=extraction.input_tokens,
                llm_output_tokens=extraction.output_tokens,
                llm_cache_read_tokens=extraction.cache_read_tokens,
                llm_requests=extraction.requests,
                llm_model=llm_model,
                attempt=state.number,
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
        # Recorded on a failed row, so `retry` can report the file as
        # orphaned if the database writes below fail.
        stored_ref = stored.ref
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
            order_number=metadata.order_number,
            payment_method=metadata.payment.method if metadata.payment else None,
            payment_last4=metadata.payment.last4 if metadata.payment else None,
            payer=metadata.payment.payer if metadata.payment else None,
            kind=metadata.kind,
            pdf_path=pdf_path_for(stored.ref),
            email_subject=raw.subject,
            email_sender=raw.sender,
            email_date=_email_date(raw.source_type, raw),
            file_name=raw.file_name,
            commit=False,
        )
        # Its source item and rendition are recorded in the same transaction
        # (evidence-store design, §7 step 5).
        record_ingest_evidence(
            conn, receipt, raw, stored, source_name=source_name, source_type=source_type
        )
        # A refund is linked to its purchase, or a purchase to its waiting
        # refunds, in the same transaction (receipt data-model design, §6).
        link_by_rule(conn, receipt)
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
            attempt=state.number,
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
        cls = _record_failure(
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
            policy=policy,
            state=state,
            stored_ref=stored_ref,
        )
        # Not exc_info: the chain can hold the model's output and its link.
        logger.warning(
            "Failed to process message %s (subject=%s, sender=%s; %s failure)\n%s",
            raw.source_id,
            raw.subject,
            raw.sender,
            cls,
            redacted_traceback(exc),
        )
        return ItemOutcome("failed", error_class=cls)


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
    policy: RetryPolicy | None = None,
) -> ItemOutcome:
    """Take the item lock, re-check, fetch, write ``started`` and process.

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

        policy = policy or RetryPolicy()
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
                policy=policy,
                state=AttemptState(),
                error_class="content",
            )
            logger.warning("Failed to fetch %s", ref.source_id, exc_info=fetch_error)
            return ItemOutcome("failed", error_class="content")

        return process_item(
            conn,
            raw,
            store=store,
            source_name=source_name,
            source_type=source_type,
            agent=agent,
            llm_model=llm_model,
            policy=policy,
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
    policy: RetryPolicy | None = None,
    control: RunControl | None = None,
) -> IngestResult:
    """Read what is new in one source and process it, one item at a time.

    The caller holds ``source_lock`` (the CLI does); it is checked before
    every cursor write. ``dry_run`` reads the new range but writes nothing:
    no rows, no locks, no cursor. ``limit`` caps the unseen items handled.
    ``control`` (``serve``'s) can stop the loop between items.
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
                if control is not None and control.should_stop():
                    logger.info("Source %s: stopping between items", source_name)
                    break
                attempted += 1

                if dry_run:
                    logger.info("Dry run: would process %s", ref.source_id)
                    result.skipped += 1
                    continue

                with _running(control, ref.source_id):
                    outcome = handle_item(
                        conn,
                        adapter,
                        ref,
                        store=store,
                        source_name=source_name,
                        source_type=source_type,
                        agent=agent,
                        llm_model=llm_model,
                        policy=policy,
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
                if outcome.stops_source:
                    result.source_error = _stop_message(outcome)
                    logger.error("Source %s: %s", source_name, result.source_error)
                    break

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


def _warn_orphans(conn: psycopg.Connection[dict[str, Any]], source_id: str) -> None:
    """Log renditions an earlier failure of this item left behind.

    The retry that just succeeded stored a new one, so these are duplicates.
    Nothing is deleted; `failures --orphans` lists them.
    """
    try:
        refs = orphaned_refs(conn, [source_id]).get(source_id, [])
    except Exception:
        conn.rollback()
        logger.warning("Can't check orphaned files for %s", source_id, exc_info=True)
        return
    for ref in refs:
        logger.warning(
            "Retry of %s succeeded; an earlier failure left %s, which no receipt "
            "references (see `receipt-index failures --orphans`)",
            source_id,
            ref,
        )


@dataclass
class RetryResult:
    """Summary of one retry pass over one source."""

    processed: int = 0
    skipped: int = 0
    failed: int = 0
    receipts: list[Receipt] = field(default_factory=list)
    # Why the pass stopped early, if it did.
    stopped: str | None = None


def run_due_retries(
    conn: psycopg.Connection[dict[str, Any]],
    due: list[IngestLogEntry],
    *,
    pool: SourcePool,
    store: EvidenceStore,
    agents: Mapping[str, Agent[None, ReceiptMetadata]],
    llm_model: str,
    policy: RetryPolicy | None = None,
    own_source: str | None = None,
    control: RunControl | None = None,
) -> RetryResult:
    """Run the items that are due again, one at a time.

    ``due`` comes from ``select_due``. For each item: take its lock (skip it
    if someone holds it), re-check that its latest row is still due, and

    - close a ``started`` row whose process died with a counted failure (it
      is retried after the normal backoff);
    - otherwise re-fetch it through the shared fetch path *before* writing
      ``started``, so an unreachable source leaves no row and uses up no
      attempt, then process it as attempt ``n``.

    ``agents`` maps ``"email"`` and ``"document"`` to their agents. The pass
    ends when ``own_source`` (the source it runs for) can't be reached, or
    on a transient or config failure. When another source of the type can't
    be reached, only the item being looked for there is left for later.
    ``control`` (``serve``'s) can stop the pass between items.
    """
    policy = policy or RetryPolicy()
    result = RetryResult()
    for entry in due:
        if control is not None and control.should_stop():
            logger.info("Retries: stopping between items")
            break
        with (
            _running(control, entry.source_id),
            item_lock(conn, entry.source_id) as locked,
        ):
            if not locked:
                logger.info("Item %s is locked; leaving it", entry.source_id)
                continue
            history = get_item_history(conn, entry.source_id)
            latest = history[0] if history else None
            if (
                latest is None
                or not is_due(latest, _now())
                or has_receipt(conn, entry.source_id)
            ):
                conn.commit()
                continue
            conn.commit()
            state = attempt_state(history)
            source_name = latest.source_name
            source_type = latest.source_type
            email_date = latest.email_date

            if latest.status == "started":
                logger.warning(
                    "Item %s has a started row and no outcome; its process "
                    "died. Recording a failure.",
                    entry.source_id,
                )
                _record_failure(
                    conn,
                    source_id=entry.source_id,
                    source_type=source_type,
                    source_name=source_name,
                    subject=latest.email_subject or "",
                    sender=latest.email_sender or "",
                    email_date=email_date,
                    exc=RuntimeError(_DIED_MID_ITEM),
                    extraction=None,
                    llm_model=llm_model,
                    policy=policy,
                    state=AttemptState(latest.attempt),
                    error_class="content",
                )
                result.failed += 1
                continue

            try:
                fetched = fetch_item(
                    pool,
                    source_id=entry.source_id,
                    source_name=source_name,
                    source_type=source_type,
                )
            except SourceDownError as exc:
                if own_source is None or own_source in pool.unavailable:
                    result.stopped = str(exc)
                    logger.error("Retries stopped: %s", exc)
                    break
                # Another source of the type is down (an item that names no
                # configured source is looked for in each): leave this item
                # for a later run, and carry on with the rest of the pass.
                logger.warning("Retry of %s left for later: %s", entry.source_id, exc)
                continue

            # A row that names no source keeps NULL, so the retry pass of
            # every source of its type still selects it.
            name = fetched.source_name or source_name
            insert_ingest_log(
                conn,
                source_id=entry.source_id,
                source_type=source_type,
                source_name=name,
                status="started",
                email_subject=latest.email_subject,
                email_sender=latest.email_sender,
                email_date=email_date,
                attempt=state.number,
            )
            if fetched.raw is None:
                exc_: Exception = fetched.item_error or LookupError(
                    "item is no longer in the source"
                )
                _record_failure(
                    conn,
                    source_id=entry.source_id,
                    source_type=source_type,
                    source_name=name,
                    subject=latest.email_subject or "",
                    sender=latest.email_sender or "",
                    email_date=email_date,
                    exc=exc_,
                    extraction=None,
                    llm_model=llm_model,
                    policy=policy,
                    state=state,
                    error_class="content",
                )
                logger.warning("Retry of %s: %s", entry.source_id, exc_)
                result.failed += 1
                continue

            raw = fetched.raw
            agent = agents["document" if raw.source_type == "gdrive" else "email"]
            outcome = process_item(
                conn,
                raw,
                store=store,
                # Found, so the source that had it is known.
                source_name=fetched.source_name or raw.source_name,
                source_type=source_type,
                agent=agent,
                llm_model=llm_model,
                policy=policy,
                state=state,
            )
        if outcome.status == "success" and outcome.receipt is not None:
            result.processed += 1
            result.receipts.append(outcome.receipt)
            _warn_orphans(conn, entry.source_id)
        elif outcome.status == "skipped":
            result.skipped += 1
        elif outcome.status == "failed":
            result.failed += 1
        if outcome.stops_source:
            result.stopped = _stop_message(outcome)
            logger.error("Retries %s", result.stopped)
            break
    return result
