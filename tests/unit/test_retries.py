"""Tests for receipt_index.retries: attempt numbers, backoff, terminal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from receipt_index.config import IngestConfig
from receipt_index.models import IngestLogEntry
from receipt_index.retries import (
    AttemptState,
    RetryPolicy,
    attempt_state,
    failure_fields,
)

_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
_POLICY = RetryPolicy()  # 3 attempts; 300, 1800, ... seconds


def _row(status: str, minutes_ago: int = 0, **kw: Any) -> IngestLogEntry:
    return IngestLogEntry.model_validate(
        {
            "id": uuid4(),
            "source_id": "<a@example.com>",
            "source_type": "imap",
            "status": status,
            "receipt_id": None,
            "vendor": None,
            "amount": None,
            "email_subject": None,
            "email_sender": None,
            "email_date": None,
            "error_message": None,
            "created_at": _NOW - timedelta(minutes=minutes_ago),
            **kw,
        }
    )


class TestDefaults:
    def test_operator_default_is_three_attempts(self) -> None:
        assert IngestConfig().retry_max_attempts == 3
        assert RetryPolicy.from_config(IngestConfig()) == _POLICY

    def test_backoff_must_cover_the_attempts(self) -> None:
        with pytest.raises(ValueError, match="needs at least 4"):
            IngestConfig(retry_max_attempts=5, retry_backoff_seconds=[1, 2, 3])
        IngestConfig(retry_max_attempts=5, retry_backoff_seconds=[1, 2, 3, 4])
        IngestConfig(retry_max_attempts=1, retry_backoff_seconds=[])

    def test_negative_backoff_is_refused(self) -> None:
        with pytest.raises(ValueError, match="0 or more"):
            IngestConfig(retry_backoff_seconds=[300, -1])


class TestAttemptState:
    def test_new_item_is_attempt_one(self) -> None:
        assert attempt_state([]) == AttemptState(1)

    def test_counted_failure_moves_to_the_next_attempt(self) -> None:
        history = [_row("started"), _row("failed", 5, attempt=2)]
        assert attempt_state(history).number == 3

    def test_queued_row_resets_to_one(self) -> None:
        history = [
            _row("queued", 1, attempt=0, counts_attempt=False),
            _row("failed", 10, attempt=3),
        ]
        assert attempt_state(history) == AttemptState(1)

    def test_uncounted_failures_keep_the_number_and_count_the_run(self) -> None:
        history = [
            _row("failed", 1, attempt=2, counts_attempt=False),
            _row("started", 2, attempt=2),
            _row("failed", 3, attempt=2, counts_attempt=False),
            _row("failed", 60, attempt=1),
        ]
        state = attempt_state(history)
        assert state.number == 2
        assert state.uncounted == 2
        assert state.uncounted_since == _NOW - timedelta(minutes=3)

    def test_uncounted_after_a_queue_starts_from_one(self) -> None:
        history = [
            _row("failed", 1, attempt=1, counts_attempt=False),
            _row("queued", 5, attempt=0, counts_attempt=False),
        ]
        assert attempt_state(history) == AttemptState(1, 1, _NOW - timedelta(minutes=1))


class TestFailureFields:
    def test_content_failure_uses_an_attempt_and_backs_off(self) -> None:
        f = failure_fields(_POLICY, AttemptState(1), "content", _NOW)
        assert (f.attempt, f.counts_attempt) == (1, True)
        assert f.next_retry_at == _NOW + timedelta(seconds=300)
        f = failure_fields(_POLICY, AttemptState(2), "content", _NOW)
        assert f.next_retry_at == _NOW + timedelta(seconds=1800)

    def test_last_attempt_is_terminal(self) -> None:
        f = failure_fields(_POLICY, AttemptState(3), "content", _NOW)
        assert f.counts_attempt
        assert f.next_retry_at is None

    def test_one_attempt_means_no_automatic_retry(self) -> None:
        policy = RetryPolicy(max_attempts=1, backoff_seconds=())
        assert (
            failure_fields(policy, AttemptState(1), "content", _NOW).next_retry_at
            is None
        )

    @pytest.mark.parametrize("cls", ["transient", "config"])
    def test_transient_and_config_use_no_attempt(self, cls: Any) -> None:
        f = failure_fields(_POLICY, AttemptState(3), cls, _NOW)
        assert (f.attempt, f.counts_attempt, f.error_class) == (3, False, cls)
        assert f.next_retry_at == _NOW + timedelta(seconds=30)

    def test_transient_delay_doubles_to_a_cap(self) -> None:
        delays = [
            failure_fields(
                _POLICY, AttemptState(1, n, _NOW), "transient", _NOW
            ).next_retry_at
            for n in (1, 2, 5, 10)
        ]
        assert delays == [
            _NOW + timedelta(seconds=60),
            _NOW + timedelta(seconds=120),
            _NOW + timedelta(seconds=900),
            _NOW + timedelta(seconds=900),
        ]

    def test_transient_cap_turns_into_a_counted_failure(self) -> None:
        since = _NOW - timedelta(seconds=_POLICY.transient_item_max_seconds)
        f = failure_fields(_POLICY, AttemptState(1, 40, since), "transient", _NOW)
        assert f.counts_attempt
        assert f.error_class == "transient"
        assert f.next_retry_at == _NOW + timedelta(seconds=300)

    def test_config_never_hits_the_transient_cap(self) -> None:
        since = _NOW - timedelta(days=3)
        f = failure_fields(_POLICY, AttemptState(1, 40, since), "config", _NOW)
        assert not f.counts_attempt
