"""Sync state, locks and incremental reads against real Postgres and GreenMail.

Covers the always-on ingest design's ticket 1 acceptance:

- a second ingest reads only new messages and downloads no processed body;
- a changed UIDVALIDITY resets the cursor and rescans headers only;
- a source held by another process makes ``ingest`` exit 3;
- the lock and cursor SQL behave as the cursor rule needs.
"""

from __future__ import annotations

import textwrap
import uuid
from datetime import date
from decimal import Decimal
from email import message_from_bytes
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import psycopg
import pytest
from click.testing import CliRunner
from imapclient import IMAPClient

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.cli import cli
from receipt_index.config import ImapSourceConfig
from receipt_index.locks import SourceLock, SourceLockLostError, item_lock
from receipt_index.models import ReceiptMetadata
from receipt_index.pipeline import run_ingest
from receipt_index.repository import (
    get_stale_started,
    get_sync_state,
    insert_ingest_log,
    is_source_id_known,
    record_poll,
    save_drive_cursor,
    save_imap_cursor,
)
from receipt_index.store import LocalFileStore

from .conftest import _DB_DSN, GREENMAIL_IMAP_PORT, seed_email

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]


def _agent() -> MagicMock:
    meta = ReceiptMetadata(
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal("12.00"),
        date=date(2025, 6, 15),
        confidence=0.95,
    )
    usage = MagicMock(input_tokens=100, output_tokens=50, cache_read_tokens=0)
    usage.requests = 1
    result = MagicMock(output=meta)
    result.usage.return_value = usage
    agent = MagicMock()
    agent.run_sync.return_value = result
    return agent


def _user(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@localhost"


def _config(user: str, name: str = "greenmail-sync") -> ImapSourceConfig:
    return ImapSourceConfig(
        name=name,
        host="localhost",
        username=user,
        password="any",  # pragma: allowlist secret
        port=GREENMAIL_IMAP_PORT,
        folder="INBOX",
        use_ssl=False,
    )


class SpyImapAdapter(ImapAdapter):
    """Records each read and which bodies were downloaded."""

    def __init__(self, config: ImapSourceConfig) -> None:
        super().__init__(config)
        self.fetched: list[str] = []
        self.reads: list[Any] = []

    def list_new(self, state: Any) -> Any:
        read = super().list_new(state)
        self.reads.append(read)
        return read

    def fetch(self, ref: Any) -> Any:
        self.fetched.append(ref.source_id)
        return super().fetch(ref)


def _ingest(conn: Conn, adapter: ImapAdapter, tmp_path: Path) -> Any:
    return run_ingest(
        conn=conn,
        adapter=adapter,
        store=LocalFileStore(tmp_path),
        source_name=adapter.name,
        source_type="imap",
        agent=_agent(),  # type: ignore[arg-type]
    )


def _seed(user: str, n: int, start: int = 1) -> None:
    for i in range(start, start + n):
        seed_email(
            to_addr=user,
            from_addr="billing@example.com",
            subject=f"Parking receipt {i}",
            text_body=f"Total: $12.00 (receipt {i})",
        )


def _count(conn: Conn, sql: str, *params: Any) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(next(iter(row.values())))


# ---------------------------------------------------------------------------
# Repository SQL
# ---------------------------------------------------------------------------


class TestSyncStateRepository:
    def test_imap_cursor_upsert_and_resets(self, pg_conn: Conn) -> None:
        assert get_sync_state(pg_conn, "mail", "INBOX") is None

        save_imap_cursor(
            pg_conn, source_name="mail", folder="INBOX", uidvalidity=7, last_uid=3
        )
        save_imap_cursor(
            pg_conn, source_name="mail", folder="INBOX", uidvalidity=7, last_uid=9
        )
        state = get_sync_state(pg_conn, "mail", "INBOX")
        assert state is not None
        assert (state.imap_uidvalidity, state.imap_last_uid, state.resets) == (7, 9, 0)

        save_imap_cursor(
            pg_conn,
            source_name="mail",
            folder="INBOX",
            uidvalidity=8,
            last_uid=0,
            reset=True,
        )
        state = get_sync_state(pg_conn, "mail", "INBOX")
        assert state is not None
        assert (state.imap_uidvalidity, state.imap_last_uid, state.resets) == (8, 0, 1)

    def test_drive_cursor_upsert(self, pg_conn: Conn) -> None:
        save_drive_cursor(
            pg_conn,
            source_name="scans",
            folder="folder-1",
            page_token="t1",
            account_id="acct-1",
        )
        save_drive_cursor(
            pg_conn,
            source_name="scans",
            folder="folder-1",
            page_token="t2",
            account_id="acct-2",
            reset=True,
        )
        state = get_sync_state(pg_conn, "scans", "folder-1")
        assert state is not None
        assert state.drive_page_token == "t2"
        assert state.drive_account_id == "acct-2"
        assert state.resets == 1
        assert state.imap_last_uid is None

    def test_record_poll_keeps_last_success_and_last_error(self, pg_conn: Conn) -> None:
        record_poll(
            pg_conn, source_name="mail", source_type="imap", folder="INBOX", error=None
        )
        first = get_sync_state(pg_conn, "mail", "INBOX")
        assert first is not None
        assert first.last_success_at is not None
        assert first.last_error is None
        assert first.imap_uidvalidity is None  # a poll row is still a first run

        record_poll(
            pg_conn,
            source_name="mail",
            source_type="imap",
            folder="INBOX",
            error="refused",
        )
        second = get_sync_state(pg_conn, "mail", "INBOX")
        assert second is not None
        assert second.last_error == "refused"
        assert second.last_error_at is not None
        assert second.last_success_at == first.last_success_at
        assert second.last_poll_at is not None
        assert first.last_poll_at is not None
        assert second.last_poll_at >= first.last_poll_at

        # A poll row then takes a cursor without violating the type checks.
        save_imap_cursor(
            pg_conn, source_name="mail", folder="INBOX", uidvalidity=1, last_uid=0
        )

    def test_is_source_id_known(self, pg_conn: Conn) -> None:
        assert not is_source_id_known(pg_conn, "<x@example.com>")
        insert_ingest_log(
            pg_conn,
            source_id="<x@example.com>",
            source_type="imap",
            source_name="mail",
            status="started",
        )
        assert is_source_id_known(pg_conn, "<x@example.com>")

    def test_stale_started(self, pg_conn: Conn) -> None:
        def row(source_id: str, status: str, age: str) -> None:
            pg_conn.execute(
                "INSERT INTO receipt.ingest_log "
                "(source_id, source_type, status, source_name, created_at) "
                "VALUES (%s, 'imap', %s, 'mail', now() - %s::interval)",
                (source_id, status, age),
            )

        row("<dead@example.com>", "started", "2 hours")
        row("<recent@example.com>", "started", "5 minutes")
        row("<finished@example.com>", "started", "3 hours")
        row("<finished@example.com>", "success", "2 hours")
        row("<retried@example.com>", "failed", "4 hours")
        row("<retried@example.com>", "started", "3 hours")
        pg_conn.commit()

        stale = get_stale_started(pg_conn, older_than_seconds=3600)
        assert [r["source_id"] for r in stale] == [
            "<retried@example.com>",
            "<dead@example.com>",
        ]


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


def _source_lock(name: str) -> SourceLock:
    return SourceLock(_DB_DSN, name)


class TestLocks:
    def test_source_lock_contention(self, pg_conn: Conn) -> None:
        name = f"lock-{uuid.uuid4().hex[:8]}"
        with _source_lock(name) as first, _source_lock(name) as second:
            assert first.acquire() is True
            assert second.acquire() is False
            first.release()
            assert second.acquire() is True

    def test_killed_connection_drops_the_lock(self, pg_conn: Conn) -> None:
        name = f"lock-{uuid.uuid4().hex[:8]}"
        with _source_lock(name) as first, _source_lock(name) as second:
            assert first.acquire() is True
            assert first._conn is not None
            pid = first._conn.info.backend_pid
            pg_conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
            pg_conn.commit()

            with pytest.raises(SourceLockLostError):
                first.check()
            assert second.acquire() is True

    def test_item_lock_skips_between_connections(self, pg_conn: Conn) -> None:
        other = psycopg.connect(_DB_DSN, row_factory=psycopg.rows.dict_row)
        try:
            with item_lock(pg_conn, "<busy@example.com>") as mine:
                assert mine is True
                with item_lock(other, "<busy@example.com>") as theirs:
                    assert theirs is False
            with item_lock(other, "<busy@example.com>") as theirs:
                assert theirs is True
        finally:
            other.close()


# ---------------------------------------------------------------------------
# GreenMail end to end
# ---------------------------------------------------------------------------


class TestIncrementalImap:
    def test_second_run_reads_only_new_messages(
        self, greenmail_available: bool, pg_conn: Conn, tmp_path: Path
    ) -> None:
        user = _user("incr")
        _seed(user, 2)

        first = SpyImapAdapter(_config(user))
        r1 = _ingest(pg_conn, first, tmp_path)
        assert r1.processed == 2
        assert len(first.fetched) == 2
        state = get_sync_state(pg_conn, "greenmail-sync", "INBOX")
        assert state is not None
        assert state.imap_uidvalidity is not None
        last_after_first = state.imap_last_uid
        assert last_after_first is not None
        assert last_after_first > 0

        _seed(user, 1, start=3)
        second = SpyImapAdapter(_config(user))
        r2 = _ingest(pg_conn, second, tmp_path)

        assert r2.processed == 1
        assert len(second.fetched) == 1  # no body re-downloaded
        # Only the new UID was listed: the search started above the cursor.
        (read,) = second.reads
        assert read.start_uid == last_after_first
        assert len(read.items) == 1
        state = get_sync_state(pg_conn, "greenmail-sync", "INBOX")
        assert state is not None
        assert state.imap_last_uid is not None
        assert state.imap_last_uid > last_after_first
        assert _count(pg_conn, "SELECT count(*) FROM receipt.receipts") == 3
        statuses = pg_conn.execute(
            "SELECT status, count(*) AS n FROM receipt.ingest_log GROUP BY status"
        ).fetchall()
        assert {r["status"]: r["n"] for r in statuses} == {"started": 3, "success": 3}

        third = SpyImapAdapter(_config(user))
        r3 = _ingest(pg_conn, third, tmp_path)
        assert (r3.processed, third.fetched) == (0, [])

    def test_uidvalidity_change_rescans_headers_only(
        self, greenmail_available: bool, pg_conn: Conn, tmp_path: Path
    ) -> None:
        user = _user("uidv")
        _seed(user, 2)
        _ingest(pg_conn, SpyImapAdapter(_config(user)), tmp_path)

        # Pretend the server rebuilt the folder: the stored value no longer
        # matches, so every stored UID is meaningless.
        pg_conn.execute(
            "UPDATE receipt.sync_state SET imap_uidvalidity = imap_uidvalidity + 1000"
        )
        pg_conn.commit()

        spy = SpyImapAdapter(_config(user))
        result = _ingest(pg_conn, spy, tmp_path)

        assert result.processed == 0
        assert spy.fetched == []  # headers only
        assert _count(pg_conn, "SELECT count(*) FROM receipt.receipts") == 2
        state = get_sync_state(pg_conn, "greenmail-sync", "INBOX")
        assert state is not None
        assert state.resets == 1
        assert state.imap_last_uid is not None
        assert state.imap_last_uid > 0

    def test_upgrade_from_full_scan_finds_nothing_unseen(
        self, greenmail_available: bool, pg_conn: Conn, tmp_path: Path
    ) -> None:
        """IDs from today's full-message parse match the header-only IDs.

        Before this change, the processed set held IDs computed from whole
        messages. The first incremental run has no sync-state row, so it
        scans headers only; it must find every one of those IDs known.
        """
        user = _user("upgrade")
        _seed(user, 3)

        client = IMAPClient("localhost", port=GREENMAIL_IMAP_PORT, ssl=False)
        try:
            client.login(user, "any")
            client.select_folder("INBOX", readonly=True)
            bodies = client.fetch(client.search("ALL"), ["BODY.PEEK[]"])
        finally:
            client.logout()
        full_ids = [
            ImapAdapter._get_message_id(message_from_bytes(data[b"BODY[]"]))
            for data in bodies.values()
        ]
        assert len(set(full_ids)) == 3
        for source_id in full_ids:
            insert_ingest_log(
                pg_conn, source_id=source_id, source_type="imap", status="success"
            )

        spy = SpyImapAdapter(_config(user))
        result = _ingest(pg_conn, spy, tmp_path)

        assert result.processed == 0
        assert spy.fetched == []


class TestConcurrentIngest:
    def test_second_ingest_exits_3_while_the_source_is_held(
        self,
        greenmail_available: bool,
        pg_conn: Conn,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The CLI builds its extraction agents before taking any lock, and the
        # Anthropic provider reads its key from the environment. No call is
        # made: the source is held, so no item runs.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
        user = _user("busy")
        name = f"busy-{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "receipt-index.yaml"
        config_path.write_text(
            textwrap.dedent(
                f"""\
                sources:
                  - name: {name}
                    type: imap
                    host: localhost
                    port: {GREENMAIL_IMAP_PORT}
                    username: {user}
                    password: any
                    folder: INBOX
                    use_ssl: false
                database:
                  url: {_DB_DSN}
                store:
                  path: {tmp_path / "store"}
                llm:
                  api_key: not-used
                """
            )
        )
        _seed(user, 1)

        with _source_lock(name) as running:
            assert running.acquire() is True
            result = CliRunner().invoke(
                cli, ["--config", str(config_path), "ingest", "--source", name]
            )

        assert result.exit_code == 3, result.output
        assert f"source {name} is being watched by a running serve" in result.output
        assert _count(pg_conn, "SELECT count(*) FROM receipt.ingest_log") == 0
