"""Retry policy: attempt numbers, backoff and terminal failures.

Pure functions over an item's ``ingest_log`` history (always-on ingest
design, section 6). The latest row for a source ID gives the item's state:

- ``queued``: requeued by ``retry --yes``; due now, and the next attempt is 1.
- ``failed`` with ``next_retry_at``: due once that time passes.
- ``failed`` without ``next_retry_at``: terminal.
- ``started`` with nobody holding its item lock: its process died.

A ``content`` failure uses up an attempt. The failure of attempt
``retry_max_attempts`` is terminal; an earlier attempt ``n`` is due again
after ``retry_backoff_seconds[n - 1]``. A ``transient`` or ``config``
failure uses up no attempt (``counts_attempt = false``) and is due again
after 30 seconds, doubling with each such failure in a row up to 15 minutes.
Once ``transient_item_max_seconds`` have passed since an item's first
transient failure (since it was last requeued), each further transient
failure uses up an attempt like a content failure, so the item can't hold up
retries for ever. Config failures, which aren't the item's fault, never
count towards that time and never use up an attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from receipt_index.config import IngestConfig
    from receipt_index.errors import ErrorClass
    from receipt_index.models import IngestLogEntry

TRANSIENT_FIRST_SECONDS = 30
TRANSIENT_MAX_SECONDS = 900


@dataclass(frozen=True)
class RetryPolicy:
    """The retry keys of the ``ingest:`` block."""

    max_attempts: int = 3
    backoff_seconds: tuple[int, ...] = (300, 1800, 7200, 43200)
    transient_item_max_seconds: int = 21600

    @classmethod
    def from_config(cls, config: IngestConfig) -> RetryPolicy:
        return cls(
            max_attempts=config.retry_max_attempts,
            backoff_seconds=tuple(config.retry_backoff_seconds),
            transient_item_max_seconds=config.transient_item_max_seconds,
        )


@dataclass(frozen=True)
class AttemptState:
    """Where an item stands before its next attempt.

    ``number`` is the attempt about to run. ``uncounted`` is how many
    uncounted failures came in a row just before it (for the transient
    delay). ``transient_since`` is when the item first failed transiently
    since it was last requeued (for the transient cap).
    """

    number: int = 1
    uncounted: int = 0
    transient_since: datetime | None = None


@dataclass(frozen=True)
class FailureFields:
    """The retry columns of one failed row."""

    attempt: int
    counts_attempt: bool
    next_retry_at: datetime | None
    error_class: ErrorClass


def attempt_state(history: Sequence[IngestLogEntry]) -> AttemptState:
    """The state of an item from its rows, newest first.

    ``started`` rows are passed over: the attempt they began is numbered by
    the failed row that ends it (or by this state, for a dead one). History
    before the latest ``queued`` row doesn't count: a requeue starts afresh.
    """
    since_queue: list[IngestLogEntry] = []
    for row in history:
        if row.status == "queued":
            break
        if row.status == "failed":
            since_queue.append(row)
        elif row.status != "started":
            # A final outcome before the failures: nothing older matters.
            break

    number = 1
    uncounted = 0
    for row in since_queue:
        if row.counts_attempt:
            number = row.attempt + 1
            break
        if uncounted == 0:
            # An uncounted failure keeps its attempt number.
            number = row.attempt
        uncounted += 1

    transient = [r.created_at for r in since_queue if r.error_class == "transient"]
    return AttemptState(number, uncounted, min(transient) if transient else None)


def failure_fields(
    policy: RetryPolicy,
    state: AttemptState,
    error_class: ErrorClass,
    now: datetime,
) -> FailureFields:
    """The retry columns for a failure of attempt ``state.number``."""
    counts = error_class == "content"
    if error_class == "transient" and state.transient_since is not None:
        elapsed = (now - state.transient_since).total_seconds()
        counts = elapsed >= policy.transient_item_max_seconds
    if not counts:
        delay = min(TRANSIENT_FIRST_SECONDS * 2**state.uncounted, TRANSIENT_MAX_SECONDS)
        return FailureFields(
            state.number, False, now + timedelta(seconds=delay), error_class
        )
    if state.number >= policy.max_attempts:
        return FailureFields(state.number, True, None, error_class)
    backoff = policy.backoff_seconds[state.number - 1]
    return FailureFields(
        state.number, True, now + timedelta(seconds=backoff), error_class
    )
