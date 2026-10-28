"""Retries against Postgres and GreenMail: the issue's acceptance.

A failed item requeued by an error substring is retried by the next ingest
and succeeds; a failure outside the filter is untouched; the dry run lists
exactly what apply requeues; each requeue is recorded. Also: a re-read never
jumps a pending retry, a locked item isn't requeued, and ``failures``
filters over the latest row per item.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import psycopg
import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.config import ImapSourceConfig
from receipt_index.fetching import SourcePool
from receipt_index.locks import item_lock
from receipt_index.models import FieldConfidence, ReceiptMetadata
from receipt_index.pipeline import run_due_retries, run_ingest
from receipt_index.repository import (
    RetryFilter,
    get_ingest_failures,
    insert_ingest_log,
    select_due,
    select_retry_candidates,
)
from receipt_index.requeue import apply_requeue
from receipt_index.retries import RetryPolicy
from receipt_index.store import LocalEvidenceStore

from .conftest import _DB_DSN, GREENMAIL_IMAP_PORT, seed_email

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

_SOURCE = "greenmail-retry"
# One attempt: failures are terminal at once, so only a requeue retries them.
_ONE_SHOT = RetryPolicy(max_attempts=1, backoff_seconds=())


def _result(vendor: str = "Acme Parking (U.S.), LLC") -> Any:
    usage = MagicMock(input_tokens=100, output_tokens=50, cache_read_tokens=0)
    usage.requests = 1
    return MagicMock(
        output=ReceiptMetadata(
            vendor=vendor,
            amount=Decimal("12.50"),
            date=date(2026, 10, 1),
            confidence=0.95,
            field_confidence=FieldConfidence(
                vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9
            ),
        ),
        usage=usage,
    )


def _config(user: str) -> ImapSourceConfig:
    return ImapSourceConfig(
        name=_SOURCE,
        host="localhost",
        username=user,
        password="any",  # pragma: allowlist secret
        port=GREENMAIL_IMAP_PORT,
        folder="INBOX",
        use_ssl=False,
    )


def _seed(user: str, subject: str) -> str:
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    seed_email(
        to_addr=user,
        from_addr="billing@example.com",
        subject=subject,
        text_body="Total: $12.50",
        message_id=message_id,
    )
    return message_id


def _ingest(
    conn: psycopg.Connection[dict[str, Any]],
    config: ImapSourceConfig,
    store: LocalEvidenceStore,
    agent: MagicMock,
    policy: RetryPolicy,
) -> tuple[Any, Any]:
    """What the one-shot `ingest` does for one source: read, then retries."""
    result = run_ingest(
        conn=conn,
        adapter=ImapAdapter(config),
        store=store,
        source_name=config.name,
        source_type="imap",
        agent=agent,
        llm_model="test-model",
        policy=policy,
    )
    due = select_due(conn, source_name=config.name, source_type="imap")
    with ExitStack() as stack:
        pool = SourcePool([config], ImapAdapter, stack)  # type: ignore[arg-type]
        retried = run_due_retries(
            conn,
            due,
            pool=pool,
            store=store,
            agents={"email": agent, "document": agent},
            llm_model="test-model",
            policy=policy,
        )
    return result, retried


def _statuses(conn: psycopg.Connection[dict[str, Any]], source_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT status FROM receipt.ingest_log WHERE source_id = %s "
        "ORDER BY created_at, id",
        (source_id,),
    ).fetchall()
    conn.commit()
    return [r["status"] for r in rows]


def test_requeue_by_error_substring_retries_only_the_match(
    greenmail_available: bool,
    pg_conn: psycopg.Connection[dict[str, Any]],
    tmp_path: Path,
) -> None:
    user = f"retry-{uuid.uuid4().hex[:8]}@localhost"
    env_failure = _seed(user, "Parking receipt")
    content_failure = _seed(user, "Newsletter")
    config = _config(user)
    store = LocalEvidenceStore(tmp_path)

    # First run: one item fails for an environmental reason, one for its
    # content. Both are terminal under a one-attempt policy.
    agent = MagicMock()
    agent.run_sync.side_effect = [
        RuntimeError("upstream connect error: renderer missing"),
        UnexpectedModelBehavior("output validation failed"),
    ]
    first, _ = _ingest(pg_conn, config, store, agent, _ONE_SHOT)
    assert first.failed == 2
    assert select_due(pg_conn, source_name=_SOURCE, source_type="imap") == []

    # Dry run and apply see the same items.
    spec = RetryFilter(error_like="UPSTREAM CONNECT")
    listed = select_retry_candidates(pg_conn, spec)
    assert [e.source_id for e in listed] == [env_failure]
    outcome = apply_requeue(pg_conn, spec, listed)
    assert outcome.requeued == [env_failure]
    assert outcome.not_requeued == []

    request = pg_conn.execute(
        "SELECT requested_by, filters, matched, requeued, "
        "(SELECT count(*) FROM receipt.ingest_log l "
        " WHERE l.retry_request_id = r.id) AS rows "
        "FROM receipt.retry_requests r WHERE id = %s",
        (outcome.request_id,),
    ).fetchone()
    pg_conn.commit()
    assert request is not None
    assert request["filters"]["error_like"] == "UPSTREAM CONNECT"
    assert (request["matched"], request["requeued"], request["rows"]) == (1, 1, 1)
    assert request["requested_by"]

    # The next ingest runs the requeued item and it succeeds.
    agent = MagicMock()
    agent.run_sync.return_value = _result()
    second, retried = _ingest(pg_conn, config, store, agent, _ONE_SHOT)
    assert second.processed == 0  # nothing new in the mailbox
    assert retried.processed == 1
    assert agent.run_sync.call_count == 1
    assert _statuses(pg_conn, env_failure) == [
        "started",
        "failed",
        "queued",
        "started",
        "success",
    ]
    attempts = pg_conn.execute(
        "SELECT attempt FROM receipt.ingest_log WHERE source_id = %s "
        "AND status IN ('started', 'success') ORDER BY created_at, id",
        (env_failure,),
    ).fetchall()
    pg_conn.commit()
    assert [r["attempt"] for r in attempts] == [1, 1, 1]

    # The failure outside the filter is untouched.
    assert _statuses(pg_conn, content_failure) == ["started", "failed"]
    terminal = get_ingest_failures(pg_conn, status="terminal")
    pg_conn.commit()
    assert [e.source_id for e in terminal] == [content_failure]


def test_automatic_retry_after_backoff(
    greenmail_available: bool,
    pg_conn: psycopg.Connection[dict[str, Any]],
    tmp_path: Path,
) -> None:
    """A content failure with attempts left is due once its time has come."""
    user = f"retry-{uuid.uuid4().hex[:8]}@localhost"
    source_id = _seed(user, "Parking receipt")
    config = _config(user)
    store = LocalEvidenceStore(tmp_path)
    policy = RetryPolicy(max_attempts=3, backoff_seconds=(0, 0))

    agent = MagicMock()
    agent.run_sync.side_effect = [RuntimeError("bad output"), _result()]
    first, retried = _ingest(pg_conn, config, store, agent, policy)
    # Backoff 0: due at once, so the same run's retry pass picks it up.
    assert first.failed == 1
    assert retried.processed == 1
    rows = pg_conn.execute(
        "SELECT status, attempt FROM receipt.ingest_log WHERE source_id = %s "
        "ORDER BY created_at, id",
        (source_id,),
    ).fetchall()
    pg_conn.commit()
    assert [(r["status"], r["attempt"]) for r in rows] == [
        ("started", 1),
        ("failed", 1),
        ("started", 2),
        ("success", 2),
    ]


def test_a_reread_never_jumps_a_pending_retry(
    greenmail_available: bool,
    pg_conn: psycopg.Connection[dict[str, Any]],
    tmp_path: Path,
) -> None:
    user = f"retry-{uuid.uuid4().hex[:8]}@localhost"
    source_id = _seed(user, "Parking receipt")
    config = _config(user)
    store = LocalEvidenceStore(tmp_path)
    policy = RetryPolicy(max_attempts=3, backoff_seconds=(3600, 3600))

    agent = MagicMock()
    agent.run_sync.side_effect = RuntimeError("bad output")
    _ingest(pg_conn, config, store, agent, policy)
    # Forget the cursor, so the next read lists the message again.
    pg_conn.execute("TRUNCATE receipt.sync_state")
    pg_conn.commit()

    agent = MagicMock()
    agent.run_sync.return_value = _result()
    second, retried = _ingest(pg_conn, config, store, agent, policy)
    assert (second.processed, retried.processed) == (0, 0)
    agent.run_sync.assert_not_called()
    assert _statuses(pg_conn, source_id) == ["started", "failed"]
    retrying = get_ingest_failures(pg_conn, status="retrying")
    pg_conn.commit()
    assert [e.source_id for e in retrying] == [source_id]


def test_locked_item_is_not_requeued(
    pg_conn: psycopg.Connection[dict[str, Any]],
) -> None:
    for sid in ("<a@example.com>", "<b@example.com>"):
        insert_ingest_log(
            pg_conn,
            source_id=sid,
            source_type="imap",
            source_name=_SOURCE,
            status="failed",
            error_message="Overloaded",
            error_class="transient",
        )
    spec = RetryFilter(error_class="transient")
    listed = select_retry_candidates(pg_conn, spec)
    assert len(listed) == 2

    other = psycopg.connect(_DB_DSN, row_factory=psycopg.rows.dict_row)
    try:
        with item_lock(other, "<b@example.com>") as locked:
            assert locked
            outcome = apply_requeue(pg_conn, spec, listed)
    finally:
        other.close()

    assert outcome.requeued == ["<a@example.com>"]
    assert outcome.not_requeued == [("<b@example.com>", "another process holds it")]
    row = pg_conn.execute(
        "SELECT matched, requeued FROM receipt.retry_requests WHERE id = %s",
        (outcome.request_id,),
    ).fetchone()
    pg_conn.commit()
    assert row == {"matched": 2, "requeued": 1}


def test_changed_item_is_not_requeued(
    pg_conn: psycopg.Connection[dict[str, Any]],
) -> None:
    insert_ingest_log(
        pg_conn,
        source_id="<a@example.com>",
        source_type="imap",
        status="failed",
        error_message="Overloaded",
    )
    spec = RetryFilter(error_like="overloaded")
    listed = select_retry_candidates(pg_conn, spec)
    # Someone else's run finished it after the listing.
    insert_ingest_log(
        pg_conn, source_id="<a@example.com>", source_type="imap", status="skipped"
    )
    outcome = apply_requeue(pg_conn, spec, listed)
    assert outcome.requeued == []
    assert outcome.not_requeued[0][0] == "<a@example.com>"


def test_failures_filters_over_the_latest_row(
    pg_conn: psycopg.Connection[dict[str, Any]],
) -> None:
    from datetime import UTC, datetime, timedelta

    later = datetime.now(tz=UTC) + timedelta(hours=1)

    def log(sid: str, status: str, **kw: Any) -> None:
        insert_ingest_log(
            pg_conn,
            source_id=sid,
            source_type="imap",
            source_name=kw.pop("source_name", _SOURCE),
            status=status,
            **kw,
        )

    log("<retrying@x>", "failed", next_retry_at=later)
    log("<terminal@x>", "failed")
    log("<recovered@x>", "failed", next_retry_at=later)
    log("<recovered@x>", "success")
    log("<skipped@x>", "skipped")
    log("<twice@x>", "failed", next_retry_at=later)
    log("<twice@x>", "failed", attempt=2, source_name="other")
    log("<queued@x>", "failed")
    log("<queued@x>", "queued", attempt=0, counts_attempt=False)

    def ids(**kw: Any) -> list[str]:
        found = sorted(e.source_id for e in get_ingest_failures(pg_conn, **kw))
        pg_conn.commit()
        return found

    assert ids() == ["<retrying@x>", "<terminal@x>", "<twice@x>"]
    assert ids(status="retrying") == ["<retrying@x>"]
    assert ids(status="terminal") == ["<terminal@x>", "<twice@x>"]
    assert ids(status="skipped") == ["<skipped@x>"]
    assert ids(status="queued") == ["<queued@x>"]
    assert ids(source_name="other") == ["<twice@x>"]
    assert ids(since=later) == []
    # The retry candidates use the same latest-row rule.
    assert [e.source_id for e in select_retry_candidates(pg_conn, RetryFilter())] == [
        "<retrying@x>",
        "<terminal@x>",
        "<twice@x>",
    ]
    pg_conn.commit()


def test_type_skips_are_not_requeued(
    pg_conn: psycopg.Connection[dict[str, Any]],
) -> None:
    """A Drive file skipped for its type can't be ingested by a retry."""
    insert_ingest_log(
        pg_conn,
        source_id="file-zip",
        source_type="gdrive",
        status="skipped",
        error_message="Skipped: unsupported file type (application/zip, a.zip)",
    )
    insert_ingest_log(
        pg_conn,
        source_id="file-pdf",
        source_type="gdrive",
        status="skipped",
        error_message="Skipped: amount=0, confidence=0.20",
    )
    insert_ingest_log(
        pg_conn, source_id="<legacy@x>", source_type="imap", status="skipped"
    )
    found = select_retry_candidates(pg_conn, RetryFilter(status="skipped"))
    pg_conn.commit()
    assert sorted(e.source_id for e in found) == ["<legacy@x>", "file-pdf"]
