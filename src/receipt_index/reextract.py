"""Re-extraction: bring existing receipts up to the current prompt and model.

The receipt data-model design, §3. Each selected receipt is read again
(from its source, else its stored rendition), extracted, and the result is
applied under fill-only rules, or ``--overwrite``. A field a person edited
(``manual_fields``) is never written. Every run appends an ``extractions``
row, so each change can be seen and undone by hand.

The rendition is never renamed here: renaming belongs to the evidence store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from receipt_index.extraction import (
    EXTRACTION_PROMPT_VERSION,
    extract_from_rendition,
    extract_metadata,
)
from receipt_index.fetching import SourceDownError, SourcePool, fetch_item
from receipt_index.locks import item_lock
from receipt_index.models import (
    FAILED_RUN_KEY,
    MIN_RECEIPT_CONFIDENCE,
    receipt_values,
    redacted_traceback,
)
from receipt_index.repository import (
    get_extraction_history,
    insert_extraction,
    is_current,
    lock_receipt,
    update_receipt_fields,
)
from receipt_index.store import rendition_ref

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from uuid import UUID

    import psycopg
    from pydantic_ai import Agent

    from receipt_index.models import (
        ExtractionResult,
        RawReceipt,
        Receipt,
        ReceiptMetadata,
    )
    from receipt_index.store import EvidenceStore

logger = logging.getLogger(__name__)

_MIN_CONFIDENCE = MIN_RECEIPT_CONFIDENCE

# A run stops after this many failures in a row: the LLM (or the store) is
# most likely down, and every further receipt would only fail too. The
# receipts it didn't reach have no row and are retried by the next run.
_MAX_CONSECUTIVE_FAILURES = 5

# Fields re-extraction never treats as empty: they are NOT NULL.
_NEVER_EMPTY = frozenset({"vendor", "currency", "receipt_date", "confidence"})

# Each field_confidence entry, and the receipt field it describes.
_CONFIDENCE_OF = {
    "field_confidence.vendor": "vendor",
    "field_confidence.amount": "amount",
    "field_confidence.paid_amount": "paid_amount",
    "field_confidence.date": "receipt_date",
}

Status = Literal[
    "applied", "unchanged", "current", "gated", "failed", "locked", "unavailable"
]
InputKind = Literal["original", "source", "rendition"]


@dataclass(frozen=True)
class History:
    """What a receipt's earlier extractions did."""

    # Whether ingest recorded this receipt's extraction. A receipt ingested
    # before the history existed has no such row: its fields came from an
    # unrecorded machine extraction and count as machine-extracted.
    ingested: bool
    # Every field any earlier extraction applied.
    applied: frozenset[str]


def _current(receipt: Receipt, name: str) -> Any:
    if name.startswith("field_confidence."):
        return receipt.field_confidence.get(name.split(".", 1)[1])
    return getattr(receipt, name)


def _same(old: Any, new: Any) -> bool:
    if isinstance(old, float) or isinstance(new, float):
        return old is not None and new is not None and abs(old - new) < 1e-6
    return bool(old == new)


def plan_changes(
    receipt: Receipt,
    metadata: ReceiptMetadata,
    history: History,
    *,
    overwrite: bool,
) -> dict[str, tuple[Any, Any]]:
    """The fields a run writes, as ``name -> (old, new)``.

    - Fill-only (the default) writes a field only when it is empty: NULL, or
      a missing ``field_confidence`` entry. ``amount`` is empty too on a
      receipt extracted under the old prompt (ingested before the history
      existed, with an empty ``field_confidence`` and no run that applied
      ``amount``), whose amount may be the payment, not the total. A gated or
      failed run changes neither, so it doesn't end that treatment.
    - ``overwrite`` also replaces a field an earlier run applied. On a
      receipt ingested before the history existed, every field counts as
      machine-extracted (only ``manual_fields`` marks a hand edit).
    - A field in ``manual_fields`` is never written, nor its confidence.
    - A field's confidence entry moves with the field. An entry is otherwise
      written only when the stored value equals the extracted one, so that
      it never describes a value the receipt doesn't hold.
    - ``overwrite`` replaces the overall ``confidence`` when anything else
      changes; fill-only never writes it.
    - A null result never clears a value, and an unchanged value isn't listed.
    """
    values = receipt_values(metadata)
    manual = set(receipt.manual_fields)
    legacy = not history.ingested
    old_prompt = (
        legacy and not receipt.field_confidence and "amount" not in history.applied
    )

    def allowed(name: str, current: Any) -> bool:
        if current is None and name not in _NEVER_EMPTY:
            return True
        if name == "amount" and old_prompt:
            return True
        return overwrite and (legacy or name in history.applied)

    changes: dict[str, tuple[Any, Any]] = {}
    for name, new in values.items():
        if name in _CONFIDENCE_OF or name == "confidence" or name in manual:
            continue
        current = _current(receipt, name)
        if new is None or _same(current, new):
            continue
        if allowed(name, current):
            changes[name] = (current, new)

    for name, of in _CONFIDENCE_OF.items():
        new = values[name]
        current = _current(receipt, name)
        if of in manual or name in manual or new is None or _same(current, new):
            continue
        if of in changes or (
            allowed(name, current) and _same(_current(receipt, of), values[of])
        ):
            changes[name] = (current, new)

    if overwrite and changes and "confidence" not in manual:
        current = receipt.confidence
        new = values["confidence"]
        if not _same(current, new) and allowed("confidence", current):
            changes["confidence"] = (current, new)
    return changes


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


@dataclass
class ReceiptOutcome:
    """What happened to one receipt."""

    receipt_id: UUID
    source_id: str
    vendor: str
    receipt_date: date
    status: Status
    input_kind: str | None = None
    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    # The extracted paid amount, applied or not, for the accuracy check.
    extracted_paid_amount: Decimal | None = None
    paid_amount_confidence: float | None = None
    extracted_confidence: float | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "receipt_id": str(self.receipt_id),
            "source_id": self.source_id,
            "vendor": self.vendor,
            "receipt_date": self.receipt_date.isoformat(),
            "status": self.status,
            "input": self.input_kind,
            "changes": {
                name: {"old": _jsonable(old), "new": _jsonable(new)}
                for name, (old, new) in self.changes.items()
            },
            "extracted_paid_amount": _jsonable(self.extracted_paid_amount),
            "paid_amount_confidence": self.paid_amount_confidence,
            "extracted_confidence": self.extracted_confidence,
            "error": self.error,
        }


@dataclass
class ReextractResult:
    """A whole run: one outcome per receipt it reached."""

    outcomes: list[ReceiptOutcome] = field(default_factory=list)
    # Set when the run stopped early (too many failures in a row).
    stopped: str | None = None

    def count(self, status: Status) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def ok(self) -> bool:
        """False when anything failed, was gated, locked or unreachable."""
        bad = {"failed", "gated", "locked", "unavailable"}
        return self.stopped is None and not any(o.status in bad for o in self.outcomes)


def _fetch_source(pool: SourcePool, receipt: Receipt) -> RawReceipt | None:
    """The receipt's item from its source, or None to use the rendition.

    An error with the item itself falls back to the rendition.
    """
    try:
        fetched = fetch_item(
            pool,
            source_id=receipt.source_id,
            source_name=receipt.source_name,
            source_type=receipt.source_type,
        )
    except SourceDownError as exc:
        raise _SourceDownError(str(exc)) from exc
    if fetched.raw is None and fetched.item_error is not None:
        logger.warning("Re-fetch of %s failed; using the rendition", receipt.source_id)
    return fetched.raw


class _SourceDownError(Exception):
    """The receipt's source can't be reached in this run."""


def _read_rendition(store: EvidenceStore, receipt: Receipt) -> bytes:
    with store.open(rendition_ref(receipt.pdf_path)) as handle:
        return handle.read()


def _outcome(receipt: Receipt, status: Status, **kw: Any) -> ReceiptOutcome:
    return ReceiptOutcome(
        receipt_id=receipt.id,
        source_id=receipt.source_id,
        vendor=receipt.vendor,
        receipt_date=receipt.receipt_date,
        status=status,
        **kw,
    )


def _record_failure(
    conn: psycopg.Connection[dict[str, Any]],
    receipt: Receipt,
    *,
    input_kind: InputKind,
    llm_model: str,
    error: str,
    extraction: ExtractionResult | None = None,
) -> None:
    """Append a failed run's row: no fields applied, the error as output."""
    conn.rollback()
    insert_extraction(
        conn,
        receipt_id=receipt.id,
        origin="reextract",
        input_kind=input_kind,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        llm_model=llm_model,
        output={FAILED_RUN_KEY: error},
        applied_fields=[],
        replaced={},
        llm_input_tokens=extraction.input_tokens if extraction else None,
        llm_output_tokens=extraction.output_tokens if extraction else None,
        llm_cache_read_tokens=extraction.cache_read_tokens if extraction else None,
        llm_requests=extraction.requests if extraction else None,
    )
    conn.commit()


def _apply(
    conn: psycopg.Connection[dict[str, Any]],
    receipt: Receipt,
    extraction: ExtractionResult,
    *,
    input_kind: InputKind,
    llm_model: str,
    overwrite: bool,
) -> ReceiptOutcome:
    """Apply one result in one transaction: the receipt and its history row.

    The receipt is read ``FOR UPDATE``, so the rules see its state now,
    including an edit made while the LLM call ran.
    """
    metadata = extraction.metadata
    extracted = {
        "extracted_paid_amount": metadata.paid_amount,
        "paid_amount_confidence": metadata.field_confidence.paid_amount,
        "extracted_confidence": metadata.confidence,
        "input_kind": input_kind,
    }
    current = lock_receipt(conn, receipt.id)
    if current is None:
        conn.rollback()
        return _outcome(receipt, "failed", error="receipt was deleted", **extracted)
    ingested, applied = get_extraction_history(conn, receipt.id)
    gated = metadata.confidence < _MIN_CONFIDENCE
    changes = (
        {}
        if gated
        else plan_changes(
            current,
            metadata,
            History(ingested=ingested, applied=frozenset(applied)),
            overwrite=overwrite,
        )
    )
    columns = {k: new for k, (_, new) in changes.items() if "." not in k}
    entries = {k.split(".", 1)[1]: new for k, (_, new) in changes.items() if "." in k}
    if columns or entries:
        update_receipt_fields(
            conn,
            receipt.id,
            columns,
            {**current.field_confidence, **entries} if entries else None,
        )
    insert_extraction(
        conn,
        receipt_id=receipt.id,
        origin="reextract",
        input_kind=input_kind,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        llm_model=llm_model,
        output=metadata.model_dump(mode="json"),
        applied_fields=list(changes),
        replaced={k: _jsonable(old) for k, (old, _) in changes.items()},
        llm_input_tokens=extraction.input_tokens,
        llm_output_tokens=extraction.output_tokens,
        llm_cache_read_tokens=extraction.cache_read_tokens,
        llm_requests=extraction.requests,
    )
    conn.commit()
    if gated:
        status: Status = "gated"
    else:
        status = "applied" if changes else "unchanged"
    return _outcome(current, status, changes=changes, **extracted)


def reextract_receipt(
    conn: psycopg.Connection[dict[str, Any]],
    receipt: Receipt,
    *,
    pool: SourcePool | None,
    store: EvidenceStore,
    agents: Mapping[str, Agent[None, ReceiptMetadata]],
    llm_model: str,
    overwrite: bool = False,
    force: bool = False,
) -> ReceiptOutcome:
    """Re-extract one receipt. ``pool`` None means ``--input rendition``.

    ``agents`` maps ``"email"`` and ``"document"`` to their agents. Takes the
    item lock on the receipt's source ID, the key ingest and retries use,
    then re-checks that the receipt is still stale (unless ``force``): an
    overlapping run may have finished it since this run selected it.
    """
    with item_lock(conn, receipt.source_id) as locked:
        if not locked:
            return _outcome(receipt, "locked")
        if not force and is_current(
            conn,
            receipt.id,
            prompt_version=EXTRACTION_PROMPT_VERSION,
            llm_model=llm_model,
        ):
            conn.commit()
            return _outcome(receipt, "current")
        # End the lock's read transaction: none is held during the network
        # fetch or the LLM call.
        conn.commit()

        raw: RawReceipt | None = None
        if pool is not None:
            try:
                raw = _fetch_source(pool, receipt)
            except _SourceDownError as exc:
                return _outcome(receipt, "unavailable", error=str(exc))

        input_kind: InputKind = "source" if raw is not None else "rendition"
        try:
            if raw is not None:
                agent = agents["document" if raw.source_type == "gdrive" else "email"]
                extraction = extract_metadata(raw, agent=agent)
            else:
                pdf = _read_rendition(store, receipt)
                extraction = extract_from_rendition(
                    pdf,
                    subject=receipt.email_subject or "",
                    sender=receipt.email_sender or "",
                    agent=agents["document"],
                )
        except Exception as exc:
            # Not exc_info: the chain can hold the model's output and its link.
            logger.warning(
                "Re-extraction of %s failed\n%s", receipt.id, redacted_traceback(exc)
            )
            error = f"{type(exc).__name__}: {exc}"
            _record_failure(
                conn, receipt, input_kind=input_kind, llm_model=llm_model, error=error
            )
            return _outcome(receipt, "failed", input_kind=input_kind, error=error)

        try:
            return _apply(
                conn,
                receipt,
                extraction,
                input_kind=input_kind,
                llm_model=llm_model,
                overwrite=overwrite,
            )
        except Exception as exc:
            logger.warning("Applying %s failed", receipt.id, exc_info=True)
            error = f"{type(exc).__name__}: {exc}"
            _record_failure(
                conn,
                receipt,
                input_kind=input_kind,
                llm_model=llm_model,
                error=error,
                extraction=extraction,
            )
            return _outcome(receipt, "failed", input_kind=input_kind, error=error)


def run_reextract(
    conn: psycopg.Connection[dict[str, Any]],
    receipts: list[Receipt],
    *,
    pool: SourcePool | None,
    store: EvidenceStore,
    agents: Mapping[str, Agent[None, ReceiptMetadata]],
    llm_model: str,
    overwrite: bool = False,
    force: bool = False,
    on_outcome: Callable[[ReceiptOutcome], None] | None = None,
) -> ReextractResult:
    """Re-extract each receipt in turn; each is its own transaction."""
    result = ReextractResult()
    failures_in_a_row = 0
    for receipt in receipts:
        outcome = reextract_receipt(
            conn,
            receipt,
            pool=pool,
            store=store,
            agents=agents,
            llm_model=llm_model,
            overwrite=overwrite,
            force=force,
        )
        result.outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)
        failures_in_a_row = failures_in_a_row + 1 if outcome.status == "failed" else 0
        if failures_in_a_row >= _MAX_CONSECUTIVE_FAILURES:
            result.stopped = (
                f"stopped after {failures_in_a_row} failures in a row "
                f"(last: {outcome.error}); the rest are retried by the next run"
            )
            logger.error("Re-extraction %s", result.stopped)
            break
    return result


def planned_input(pool: SourcePool | None, receipt: Receipt) -> str:
    """What a dry run says a receipt would be read from."""
    if pool is None or not pool.candidates(receipt.source_name, receipt.source_type):
        return "rendition"
    return "source, else rendition"
