"""The queries `status` and the dead-man ping read, against Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from receipt_index.repository import (
    count_due_retries,
    count_ingest_failures,
    insert_ingest_log,
    list_sync_state,
    record_poll,
    save_imap_cursor,
)

if TYPE_CHECKING:
    import psycopg

pytestmark = pytest.mark.integration


def _fail(
    conn: psycopg.Connection[dict[str, Any]],
    source_id: str,
    *,
    next_retry_at: datetime | None,
    age: timedelta | None = None,
) -> None:
    entry = insert_ingest_log(
        conn,
        source_id=source_id,
        source_type="imap",
        source_name="mail",
        status="failed",
        error_message="boom",
        next_retry_at=next_retry_at,
    )
    if age is not None:
        conn.execute(
            "UPDATE receipt.ingest_log SET created_at = now() - %(age)s "
            "WHERE id = %(id)s",
            {"age": age, "id": entry.id},
        )
        conn.commit()


def test_list_sync_state_returns_every_row(
    pg_conn: psycopg.Connection[dict[str, Any]],
) -> None:
    save_imap_cursor(
        pg_conn, source_name="mail", folder="INBOX", uidvalidity=7, last_uid=42
    )
    record_poll(
        pg_conn, source_name="mail", source_type="imap", folder="INBOX", error=None
    )
    record_poll(
        pg_conn, source_name="gone", source_type="imap", folder="INBOX", error="x"
    )
    rows = list_sync_state(pg_conn)
    assert [(r.source_name, r.folder) for r in rows] == [
        ("gone", "INBOX"),
        ("mail", "INBOX"),
    ]
    gone, mail = rows
    assert mail.imap_last_uid == 42
    assert mail.last_success_at is not None
    assert gone.last_success_at is None
    assert gone.last_error == "x"


def test_terminal_within_the_alert_window(
    pg_conn: psycopg.Connection[dict[str, Any]],
) -> None:
    now = datetime.now(UTC)
    _fail(pg_conn, "<new-terminal@example.com>", next_retry_at=None)
    _fail(
        pg_conn,
        "<old-terminal@example.com>",
        next_retry_at=None,
        age=timedelta(days=2),
    )
    _fail(pg_conn, "<retrying@example.com>", next_retry_at=now + timedelta(hours=1))
    # Terminal, then requeued by `retry`: no longer terminal.
    _fail(pg_conn, "<requeued@example.com>", next_retry_at=None)
    insert_ingest_log(
        pg_conn,
        source_id="<requeued@example.com>",
        source_type="imap",
        source_name="mail",
        status="queued",
    )

    window = now - timedelta(seconds=86400)
    assert count_ingest_failures(pg_conn, status="terminal", since=window) == 1
    assert count_ingest_failures(pg_conn, status="terminal") == 2
    # Past the window, the alert clears with no acknowledgement step.
    later = now + timedelta(seconds=86400 + 60)
    assert (
        count_ingest_failures(
            pg_conn, status="terminal", since=later - timedelta(seconds=86400)
        )
        == 0
    )


def test_count_due_retries_counts_only_due_failures(
    pg_conn: psycopg.Connection[dict[str, Any]],
) -> None:
    now = datetime.now(UTC)
    _fail(pg_conn, "<due@example.com>", next_retry_at=now - timedelta(minutes=1))
    _fail(pg_conn, "<later@example.com>", next_retry_at=now + timedelta(hours=1))
    _fail(pg_conn, "<terminal@example.com>", next_retry_at=None)
    assert count_due_retries(pg_conn) == 1
    assert count_ingest_failures(pg_conn, status="retrying") == 2
