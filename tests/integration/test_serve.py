"""``serve`` end to end, against Postgres and GreenMail.

The service runs in-process with a mock extraction agent. GreenMail 2.1.2
supports IDLE: a message delivered after start-up is indexed through an IDLE
wake-up, with the safety poll set far beyond the test's length. Drive has no
emulator here, so its case uses a fake adapter whose folder gains a file
while the service runs; the poll timer must pick it up without a restart.

Every wait is bounded, so a bug fails a test instead of holding the shared
integration lock.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Self
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import dict_row

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.config import GdriveSourceConfig, ImapSourceConfig
from receipt_index.db import get_connection
from receipt_index.errors import classify
from receipt_index.locks import SourceLock
from receipt_index.models import (
    FieldConfidence,
    ItemRef,
    RawReceipt,
    ReceiptMetadata,
    SourceRead,
)
from receipt_index.retries import RetryPolicy
from receipt_index.service import ServeTimings, Service
from receipt_index.store import LocalEvidenceStore
from tests.pdfs import make_pdf

from .conftest import _DB_DSN, GREENMAIL_IMAP_PORT, seed_email

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from types import TracebackType

    from receipt_index.config import SourceConfig
    from receipt_index.models import SyncState

pytestmark = pytest.mark.integration

_WAIT = 30.0  # the longest any step waits
_FIELD_CONFIDENCE = FieldConfidence(vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9)


def _agent(run: Callable[..., Any] | None = None) -> MagicMock:
    meta = ReceiptMetadata(
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal("12.00"),
        date=date(2025, 6, 15),
        confidence=0.95,
        field_confidence=_FIELD_CONFIDENCE,
    )
    usage = MagicMock(input_tokens=100, output_tokens=50, cache_read_tokens=0)
    usage.requests = 1
    result = MagicMock(output=meta)
    result.usage = usage
    agent = MagicMock()
    if run is None:
        agent.run_sync.return_value = result
    else:

        def side_effect(*args: Any, **kwargs: Any) -> Any:
            run()
            return result

        agent.run_sync.side_effect = side_effect
    return agent


def _rows(sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(_DB_DSN, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def _statuses(source_id: str) -> list[str]:
    rows = _rows(
        "SELECT status FROM receipt.ingest_log WHERE source_id = %s "
        "ORDER BY created_at, id",
        source_id,
    )
    return [r["status"] for r in rows]


def _until(check: Callable[[], bool], timeout: float = _WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return check()


def _imap_config(user: str) -> ImapSourceConfig:
    return ImapSourceConfig(
        name="greenmail-serve",
        host="localhost",
        username=user,
        password="any",  # pragma: allowlist secret
        port=GREENMAIL_IMAP_PORT,
        folder="INBOX",
        use_ssl=False,
    )


class _Running:
    """A started Service and its source locks; stops and releases on exit."""

    def __init__(
        self,
        sources: list[SourceConfig],
        tmp_path: Path,
        *,
        agent: MagicMock,
        adapter_factory: Callable[[SourceConfig], Any],
        poll_seconds: float = 3600,
    ) -> None:
        self.locks = {s.name: SourceLock(_DB_DSN, s.name) for s in sources}
        for lock in self.locks.values():
            assert lock.acquire()
        self.exits: list[int] = []
        self.service = Service(
            sources=sources,
            all_sources=sources,
            locks=self.locks,
            timings=ServeTimings(
                idle_reissue_seconds=600,
                imap_poll_seconds=3600,  # no safety poll during the test
                drive_poll_seconds=poll_seconds,
                item_timeout_seconds=600,
                retry_seconds=3600,
                join_seconds=10,
            ),
            open_conn=lambda: get_connection(_DB_DSN, statement_timeout_seconds=60),
            adapter_factory=adapter_factory,
            agents={"email": agent, "document": agent},
            store=LocalEvidenceStore(tmp_path),
            llm_model="test-model",
            policy=RetryPolicy(),
            exit_fn=self.exits.append,
        )

    def __enter__(self) -> Self:
        self.service.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.service.stop()
            self.service.shutdown()
        finally:
            for lock in self.locks.values():
                lock.release()


def _imap_factory(config: SourceConfig) -> Any:
    assert isinstance(config, ImapSourceConfig)
    return ImapAdapter(config)


def _watcher_in_idle(running: _Running) -> bool:
    """Whether the catch-up poll has run, so the next mail must come by IDLE."""
    rows = _rows(
        "SELECT last_poll_at FROM receipt.sync_state WHERE source_name = %s",
        "greenmail-serve",
    )
    return bool(rows) and rows[0]["last_poll_at"] is not None


class TestServeImap:
    def test_mail_delivered_while_idle_is_indexed(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        greenmail_available: bool,
        tmp_path: Path,
    ) -> None:
        user = f"serve-{uuid.uuid4().hex[:8]}@localhost"
        seed_email(to_addr=user, subject="Before start", message_id="<pre@example.com>")
        with _Running(
            [_imap_config(user)],
            tmp_path,
            agent=_agent(),
            adapter_factory=_imap_factory,
        ) as running:
            # The catch-up poll at start-up indexes what was already there.
            assert _until(lambda: _statuses("<pre@example.com>")[-1:] == ["success"])
            assert _until(lambda: _watcher_in_idle(running))
            time.sleep(1.0)  # let the watcher settle into IDLE

            seed_email(
                to_addr=user, subject="While idle", message_id="<idle@example.com>"
            )
            assert _until(
                lambda: _statuses("<idle@example.com>")[-1:] == ["success"]
            ), "a message delivered during IDLE wasn't indexed"

            start = time.monotonic()
            running.service.stop()
            assert running.service.shutdown()
            assert time.monotonic() - start < 10.0
        assert running.exits == []
        dangling = _rows(
            "SELECT source_id FROM receipt.ingest_log l WHERE status = 'started' "
            "AND NOT EXISTS (SELECT 1 FROM receipt.ingest_log o "
            "WHERE o.source_id = l.source_id AND o.status <> 'started')"
        )
        assert dangling == []

    def test_sigterm_mid_item_lets_the_item_finish(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        greenmail_available: bool,
        tmp_path: Path,
    ) -> None:
        user = f"serve-{uuid.uuid4().hex[:8]}@localhost"
        for n in (1, 2):
            seed_email(
                to_addr=user, subject=f"R{n}", message_id=f"<mid-{n}@example.com>"
            )
        in_extraction, release = threading.Event(), threading.Event()

        def slow() -> None:
            in_extraction.set()
            assert release.wait(_WAIT), "the test never released the item"

        with _Running(
            [_imap_config(user)],
            tmp_path,
            agent=_agent(slow),
            adapter_factory=_imap_factory,
        ) as running:
            assert in_extraction.wait(_WAIT)
            running.service.stop()  # what the SIGTERM handler calls
            release.set()
            assert running.service.shutdown()

        # The item in hand finished; the next one was never started.
        assert _statuses("<mid-1@example.com>") == ["started", "success"]
        assert _statuses("<mid-2@example.com>") == []


class _FakeDrive:
    """A Drive folder that gains files while the test runs.

    ``list_new`` returns every file not yet passed by the saved token, as
    the changes feed would, and a token that covers them.
    """

    folder_files: ClassVar[list[str]] = []
    lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, config: GdriveSourceConfig) -> None:
        self.config = config
        self.source_type = "gdrive"

    @property
    def folder(self) -> str:
        return self.config.folder_id

    def list_new(self, state: SyncState | None) -> SourceRead:
        seen = int(state.drive_page_token) if state and state.drive_page_token else 0
        with self.lock:
            files = list(self.folder_files)
        refs = [
            ItemRef(
                source_id=fid,
                source_type="gdrive",
                file_name=f"{fid}.pdf",
                drive_meta={"id": fid, "mimeType": "application/pdf"},
            )
            for fid in files[seen:]
        ]
        return SourceRead(
            items=refs, drive_page_token=str(len(files)), drive_account_id="acct-1"
        )

    def fetch(self, ref: ItemRef) -> RawReceipt | None:
        return RawReceipt(
            source_id=ref.source_id,
            source_name=self.config.name,
            source_type="gdrive",
            date=datetime.now(tz=UTC),
            file_name=ref.file_name,
            file_content=make_pdf("Acme Parking receipt total 12.00"),
            file_content_type="application/pdf",
        )

    def fetch_by_id(self, source_id: str) -> RawReceipt | None:
        return None

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def drive_folder() -> Iterator[list[str]]:
    _FakeDrive.folder_files = []
    yield _FakeDrive.folder_files
    _FakeDrive.folder_files = []


class TestServeDrive:
    def test_a_new_file_is_picked_up_by_the_next_poll(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
        drive_folder: list[str],
    ) -> None:
        config = GdriveSourceConfig(
            name="drive-serve", folder_id="folder-1", token_json="{}"
        )
        drive_folder.append("file-1")

        def factory(cfg: SourceConfig) -> Any:
            assert isinstance(cfg, GdriveSourceConfig)
            return _FakeDrive(cfg)

        with _Running(
            [config],
            tmp_path,
            agent=_agent(),
            adapter_factory=factory,
            poll_seconds=0.5,
        ):
            assert _until(lambda: _statuses("file-1")[-1:] == ["success"])
            with _FakeDrive.lock:
                drive_folder.append("file-2")
            assert _until(
                lambda: _statuses("file-2")[-1:] == ["success"]
            ), "a file added while serve ran wasn't indexed"
        token = _rows(
            "SELECT drive_page_token FROM receipt.sync_state WHERE source_name = %s",
            "drive-serve",
        )
        assert token[0]["drive_page_token"] == "2"


class TestStatementTimeout:
    def test_a_slow_statement_is_cancelled_and_transient(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        with get_connection(_DB_DSN, statement_timeout_seconds=0.5) as conn:
            with pytest.raises(psycopg.errors.QueryCanceled) as info:
                conn.execute("SELECT pg_sleep(3)")
            assert classify(info.value) == "transient"

    def test_without_the_option_there_is_no_timeout(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        with get_connection(_DB_DSN) as conn:
            row = conn.execute("SHOW statement_timeout").fetchone()
        assert row is not None
        assert row["statement_timeout"] == "0"
