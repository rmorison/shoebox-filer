"""Tests for receipt_index.pipeline.

A fake repository records every row and cursor write in memory, and a fake
adapter serves refs and bodies, so each test can check what was written and
how far the cursor moved.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelAuthenticationError,
    ModelOverloadedError,
    UnexpectedModelBehavior,
)

from receipt_index import pipeline
from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.locks import SourceLockLostError
from receipt_index.models import (
    ExtractionResult,
    FieldConfidence,
    ItemRef,
    Payment,
    RawReceipt,
    Receipt,
    ReceiptMetadata,
    SourceRead,
    SyncState,
)
from receipt_index.pipeline import IngestResult, run_ingest
from receipt_index.renderer import Rendition
from receipt_index.store import StoredFile

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)

_RECEIPT_ROW: dict[str, Any] = {
    "id": UUID("019572a0-0000-7000-8000-000000000001"),
    "source_id": "<msg-1@example.com>",
    "source_type": "imap",
    "source_name": "personal-email",
    "vendor": "Amazon",
    "amount": Decimal("42.99"),
    "currency": "USD",
    "receipt_date": date(2025, 6, 15),
    "description": "Python Cookbook",
    "confidence": 0.95,
    "pdf_path": "2025/06/2025-06-15__amazon__42.99.pdf",
    "email_subject": "Your Amazon.com order",
    "email_sender": "no-reply@amazon.com",
    "email_date": datetime(2025, 6, 15, 10, 30, 0, tzinfo=UTC),
    "file_name": None,
    "created_at": _NOW,
    "updated_at": _NOW,
}


_FIELD_CONFIDENCE = FieldConfidence(vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9)


def _extraction(confidence: float = 0.95) -> ExtractionResult:
    return ExtractionResult(
        metadata=ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("42.99"),
            currency="USD",
            date=date(2025, 6, 15),
            description="Python Cookbook",
            confidence=confidence,
            field_confidence=_FIELD_CONFIDENCE,
        ),
        input_tokens=150,
        output_tokens=50,
        cache_read_tokens=0,
        requests=1,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeRepo:
    """In-memory stand-in for the repository functions the pipeline calls."""

    state: SyncState | None = None
    rows: list[tuple[str, str]] = field(default_factory=list)  # (source_id, status)
    row_kwargs: list[dict[str, Any]] = field(default_factory=list)
    receipts: set[str] = field(default_factory=set)
    receipt_kwargs: list[dict[str, Any]] = field(default_factory=list)
    imap_saves: list[tuple[int, int, bool]] = field(default_factory=list)
    drive_saves: list[tuple[str, str, bool]] = field(default_factory=list)
    polls: list[str | None] = field(default_factory=list)
    locked: set[str] = field(default_factory=set)
    # Became known between listing and locking (finished by someone else).
    known_after_listing: set[str] = field(default_factory=set)
    fail_on_started: set[str] = field(default_factory=set)
    extractions: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    renditions: list[dict[str, Any]] = field(default_factory=list)
    # Every write in order, to check what commits with what.
    writes: list[str] = field(default_factory=list)
    # Receipts the refund linking rule ran on.
    linked: list[Receipt] = field(default_factory=list)

    def known(self) -> set[str]:
        return {sid for sid, _ in self.rows} | self.receipts

    # -- repository API ---------------------------------------------------

    def get_sync_state(self, conn: Any, source_name: str, folder: str) -> Any:
        return self.state

    def record_poll(self, conn: Any, *, error: str | None, **_: Any) -> None:
        self.polls.append(error)

    def get_processed_source_ids(self, conn: Any) -> set[str]:
        return self.known()

    def is_source_id_known(self, conn: Any, source_id: str) -> bool:
        return source_id in self.known() or source_id in self.known_after_listing

    def insert_ingest_log(
        self, conn: Any, *, source_id: str, status: str, **kwargs: Any
    ) -> None:
        if status == "started" and source_id in self.fail_on_started:
            raise RuntimeError("database went away")
        self.rows.append((source_id, status))
        self.writes.append(f"ingest_log:{status}")
        self.row_kwargs.append({"source_id": source_id, "status": status, **kwargs})

    def insert_receipt(self, conn: Any, *, source_id: str, **kwargs: Any) -> Receipt:
        self.receipts.add(source_id)
        self.writes.append("receipt")
        self.receipt_kwargs.append({"source_id": source_id, **kwargs})
        return Receipt.model_validate({**_RECEIPT_ROW, "source_id": source_id})

    def insert_provenance(self, conn: Any, *, source_id: str, **kwargs: Any) -> None:
        self.writes.append("provenance")
        self.provenance.append({"source_id": source_id, **kwargs})

    def record_rendition(self, conn: Any, **kwargs: Any) -> None:
        self.writes.append("rendition")
        self.renditions.append(kwargs)

    def link_by_rule(self, conn: Any, receipt: Receipt) -> list[Any]:
        self.writes.append("link_rule")
        self.linked.append(receipt)
        return []

    def insert_extraction(self, conn: Any, **kwargs: Any) -> None:
        self.writes.append("extraction")
        self.extractions.append(kwargs)

    def save_imap_cursor(
        self, conn: Any, *, uidvalidity: int, last_uid: int, reset: bool, **_: Any
    ) -> None:
        self.imap_saves.append((uidvalidity, last_uid, reset))

    def save_drive_cursor(
        self, conn: Any, *, page_token: str, account_id: str, reset: bool, **_: Any
    ) -> None:
        self.drive_saves.append((page_token, account_id, reset))

    @contextmanager
    def item_lock(self, conn: Any, source_id: str) -> Iterator[bool]:
        yield source_id not in self.locked

    def statuses(self, source_id: str) -> list[str]:
        return [st for sid, st in self.rows if sid == source_id]


class FakeAdapter:
    """Serves a fixed SourceRead and bodies by source ID."""

    def __init__(
        self,
        read: SourceRead,
        *,
        source_type: str = "imap",
        fail_fetch: set[str] | None = None,
        gone: set[str] | None = None,
        list_error: Exception | None = None,
        unavailable_from: str | None = None,
    ) -> None:
        self.read = read
        self.source_type = source_type
        self.fail_fetch = fail_fetch or set()
        self.gone = gone or set()
        self.list_error = list_error
        self.unavailable_from = unavailable_from
        self.down = False
        self.fetched: list[str] = []
        self.closed = False

    @property
    def folder(self) -> str:
        return "INBOX"

    def list_new(self, state: SyncState | None) -> SourceRead:
        if self.list_error is not None:
            raise self.list_error
        return self.read

    def fetch(self, ref: ItemRef) -> RawReceipt | None:
        self.fetched.append(ref.source_id)
        if ref.source_id == self.unavailable_from:
            self.down = True
        if self.down:
            raise SourceUnavailableError("IMAP connection to mail.example.com failed")
        if ref.source_id in self.fail_fetch:
            raise ValueError("cannot parse message")
        if ref.source_id in self.gone:
            return None
        return RawReceipt(
            source_id=ref.source_id,
            source_name="personal-email",
            source_type=self.source_type,
            date=datetime(2025, 6, 15, 10, 30, 0, tzinfo=UTC),
            subject=ref.subject,
            sender=ref.sender,
            text_body="Order Total: $42.99",
        )

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _imap_ref(uid: int) -> ItemRef:
    return ItemRef(
        source_id=f"<msg-{uid}@example.com>",
        source_type="imap",
        subject=f"Receipt {uid}",
        sender="shop@example.com",
        date=datetime(2025, 6, 15, 10, 30, 0, tzinfo=UTC),
        imap_uid=uid,
    )


def _imap_read(
    uids: list[int],
    *,
    start_uid: int = 0,
    uidvalidity: int = 7,
    reset: str | None = None,
    searched: list[int] | None = None,
) -> SourceRead:
    return SourceRead(
        items=[_imap_ref(u) for u in uids],
        reset=reset,
        uidvalidity=uidvalidity,
        start_uid=start_uid,
        searched_uids=searched if searched is not None else list(uids),
    )


def _drive_ref(file_id: str, *, skip: str | None = None) -> ItemRef:
    return ItemRef(
        source_id=file_id,
        source_type="gdrive",
        file_name=f"{file_id}.pdf",
        drive_meta={"id": file_id, "mimeType": "application/zip" if skip else "pdf"},
        skip_reason=skip,
    )


def _drive_read(
    ids: list[str],
    *,
    held_back: bool = False,
    reset: str | None = None,
    skips: set[str] | None = None,
) -> SourceRead:
    skips = skips or set()
    return SourceRead(
        items=[
            _drive_ref(i, skip="unsupported file type" if i in skips else None)
            for i in ids
        ],
        reset=reset,
        drive_page_token="tok-new",
        drive_account_id="acct-1",
        held_back=held_back,
    )


def _imap_state(uidvalidity: int, last_uid: int) -> SyncState:
    return SyncState.model_validate(
        {
            "id": "019572a0-0000-7000-8000-0000000000aa",
            "source_name": "personal-email",
            "source_type": "imap",
            "folder": "INBOX",
            "imap_uidvalidity": uidvalidity,
            "imap_last_uid": last_uid,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    )


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    fake = FakeRepo()
    for name in (
        "get_sync_state",
        "record_poll",
        "get_processed_source_ids",
        "is_source_id_known",
        "insert_ingest_log",
        "insert_receipt",
        "insert_provenance",
        "record_rendition",
        "insert_extraction",
        "link_by_rule",
        "save_imap_cursor",
        "save_drive_cursor",
        "item_lock",
    ):
        monkeypatch.setattr(pipeline, name, getattr(fake, name))
    return fake


@pytest.fixture
def extract(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(return_value=_extraction())
    monkeypatch.setattr(pipeline, "extract_metadata", mock)
    monkeypatch.setattr(
        pipeline, "render_rendition", MagicMock(return_value=Rendition(pdf=b"%PDF"))
    )
    return mock


def _store() -> MagicMock:
    store = MagicMock()
    store.put.return_value = StoredFile(
        sha256=b"\x00" * 32,
        byte_size=4,
        media_type="application/pdf",
        ref="local:renditions/2025/06/2025-06-15__amazon__42.99.pdf",
    )
    return store


def _run(adapter: FakeAdapter, **kwargs: Any) -> IngestResult:
    kwargs.setdefault("llm_model", "test-model")
    return run_ingest(
        conn=MagicMock(),
        adapter=adapter,  # type: ignore[arg-type]
        store=_store(),
        source_name="personal-email",
        source_type=adapter.source_type,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


class TestProcessing:
    def test_processes_receipt(self, repo: FakeRepo, extract: MagicMock) -> None:
        adapter = FakeAdapter(_imap_read([1]))
        result = _run(adapter, llm_model="m")

        assert result.processed == 1
        assert len(result.receipts) == 1
        assert repo.statuses("<msg-1@example.com>") == ["started", "success"]
        assert all(k["source_name"] == "personal-email" for k in repo.row_kwargs)
        assert adapter.closed
        assert repo.polls == [None]

    def test_low_confidence_is_skipped(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.return_value = _extraction(confidence=0.2)
        result = _run(FakeAdapter(_imap_read([1])))
        assert result.skipped == 1
        assert repo.statuses("<msg-1@example.com>") == ["started", "skipped"]

    def test_passes_agent_to_extract(self, repo: FakeRepo, extract: MagicMock) -> None:
        agent = MagicMock()
        _run(FakeAdapter(_imap_read([1])), agent=agent)
        assert extract.call_args.kwargs["agent"] is agent

    def test_per_item_failure_continues(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.side_effect = RuntimeError("LLM error")
        result = _run(FakeAdapter(_imap_read([1, 2])))

        assert result.failed == 2
        failed = [k for k in repo.row_kwargs if k["status"] == "failed"]
        assert len(failed) == 2
        assert all("LLM error" in k["error_message"] for k in failed)
        assert repo.imap_saves[-1] == (7, 2, False)

    def test_second_run_downloads_no_known_body(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.rows = [("<msg-1@example.com>", "success")]
        repo.receipts = {"<msg-2@example.com>"}
        adapter = FakeAdapter(_imap_read([1, 2, 3]))

        result = _run(adapter)

        assert adapter.fetched == ["<msg-3@example.com>"]
        assert result.processed == 1
        assert repo.imap_saves[-1] == (7, 3, False)

    def test_dry_run_writes_nothing(self, repo: FakeRepo, extract: MagicMock) -> None:
        adapter = FakeAdapter(_imap_read([1, 2]))
        result = _run(adapter, dry_run=True)

        assert result.skipped == 2
        assert result.processed == 0
        assert repo.rows == []
        assert repo.imap_saves == []
        assert repo.polls == []
        assert adapter.fetched == []

    def test_limit_caps_unseen_items(self, repo: FakeRepo, extract: MagicMock) -> None:
        result = _run(FakeAdapter(_imap_read([1, 2, 3, 4, 5])), limit=2)

        assert result.processed == 2
        # The cursor stops at the last handled item.
        assert repo.imap_saves[-1] == (7, 2, False)

    def test_recheck_after_lock_drops_a_finished_item(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.known_after_listing = {"<msg-1@example.com>"}
        adapter = FakeAdapter(_imap_read([1, 2]))

        result = _run(adapter)

        assert adapter.fetched == ["<msg-2@example.com>"]
        assert repo.statuses("<msg-1@example.com>") == []
        assert result.processed == 1
        assert repo.imap_saves[-1] == (7, 2, False)


class TestFetchErrors:
    def test_fetch_raising_after_started_writes_failed(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        adapter = FakeAdapter(_imap_read([1, 2]), fail_fetch={"<msg-1@example.com>"})

        result = _run(adapter)

        assert repo.statuses("<msg-1@example.com>") == ["started", "failed"]
        failed = next(k for k in repo.row_kwargs if k["status"] == "failed")
        assert "cannot parse message" in failed["error_message"]
        assert result.failed == 1
        assert result.processed == 1  # the next item still ran
        assert repo.imap_saves[-1] == (7, 2, False)

    def test_unreachable_source_stops_without_failing_items(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        """A dropped connection is the source's fault, not the items'."""
        repo.state = _imap_state(7, 0)
        adapter = FakeAdapter(
            _imap_read([1, 2, 3]), unavailable_from="<msg-2@example.com>"
        )

        result = _run(adapter)

        assert repo.statuses("<msg-1@example.com>") == ["started", "success"]
        # No row for the item that hit the outage, or for any later item.
        assert repo.statuses("<msg-2@example.com>") == []
        assert repo.statuses("<msg-3@example.com>") == []
        assert result.failed == 0
        assert result.source_error is not None
        assert "connection" in result.source_error
        assert repo.polls[-1] == result.source_error
        # The cursor stays below the unreached items, so the next run retries.
        assert repo.imap_saves[-1] == (7, 1, False)
        assert adapter.fetched == ["<msg-1@example.com>", "<msg-2@example.com>"]

    def test_read_transaction_ends_before_the_download(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        events: list[str] = []
        conn = MagicMock()
        conn.commit.side_effect = lambda: events.append("commit")
        adapter = FakeAdapter(_imap_read([1]))
        real_fetch = adapter.fetch

        def fetch(ref: ItemRef) -> RawReceipt | None:
            events.append("fetch")
            return real_fetch(ref)

        adapter.fetch = fetch  # type: ignore[method-assign]
        run_ingest(
            conn=conn,
            adapter=adapter,  # type: ignore[arg-type]
            store=_store(),
            source_name="personal-email",
            source_type="imap",
            llm_model="test-model",
        )
        assert events[:2] == ["commit", "fetch"]

    def test_unreachable_drive_keeps_the_token(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        adapter = FakeAdapter(
            _drive_read(["f1", "f2"]), source_type="gdrive", unavailable_from="f2"
        )
        result = _run(adapter)
        assert result.source_error is not None
        assert repo.statuses("f2") == []
        assert repo.drive_saves == []

    def test_gone_item_writes_failed(self, repo: FakeRepo, extract: MagicMock) -> None:
        adapter = FakeAdapter(_imap_read([1]), gone={"<msg-1@example.com>"})
        result = _run(adapter)
        assert repo.statuses("<msg-1@example.com>") == ["started", "failed"]
        assert result.failed == 1


# ---------------------------------------------------------------------------
# The cursor rule
# ---------------------------------------------------------------------------


class TestImapCursor:
    def test_crash_before_started_leaves_cursor_below_the_item(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.state = _imap_state(7, 0)
        repo.fail_on_started = {"<msg-2@example.com>"}

        with pytest.raises(RuntimeError, match="database went away"):
            _run(FakeAdapter(_imap_read([1, 2, 3])))

        assert repo.imap_saves == [(7, 1, False)]

    def test_locked_item_holds_the_cursor(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.state = _imap_state(7, 0)
        repo.locked = {"<msg-2@example.com>"}

        result = _run(FakeAdapter(_imap_read([1, 2, 3])))

        assert result.processed == 2  # items 1 and 3 still ran
        assert max(uid for _, uid, _ in repo.imap_saves) == 1

    def test_uid_without_a_ref_holds_the_cursor(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.state = _imap_state(7, 0)
        read = _imap_read([1, 3], searched=[1, 2, 3])

        _run(FakeAdapter(read))

        assert max(uid for _, uid, _ in repo.imap_saves) == 1

    def test_nothing_unseen_moves_to_the_highest_uid(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.state = _imap_state(7, 4)
        repo.rows = [
            ("<msg-5@example.com>", "success"),
            ("<msg-9@example.com>", "skipped"),
        ]

        _run(FakeAdapter(_imap_read([5, 9], start_uid=4)))

        assert repo.imap_saves[-1] == (7, 9, False)

    def test_first_run_saves_the_numbering_at_once(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.locked = {"<msg-1@example.com>"}
        _run(FakeAdapter(_imap_read([1, 2])))
        assert repo.imap_saves == [(7, 0, False)]

    def test_reset_never_pairs_new_uidvalidity_with_old_uid(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.state = _imap_state(7, 500)
        repo.locked = {"<msg-1@example.com>"}
        read = _imap_read([1, 2], uidvalidity=99, reset="uidvalidity changed")

        _run(FakeAdapter(read))

        assert repo.imap_saves == [(99, 0, True)]

    def test_lost_source_lock_stops_before_any_cursor_write(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.state = _imap_state(7, 0)
        lock = MagicMock()
        lock.check.side_effect = SourceLockLostError("lost the source lock")

        result = _run(FakeAdapter(_imap_read([1, 2])), source_lock=lock)

        assert repo.imap_saves == []
        assert result.source_error == "lost the source lock"
        assert repo.polls == [None, "lost the source lock"]
        # Item 1 ran and has its outcome; the run stopped before item 2.
        assert repo.statuses("<msg-1@example.com>") == ["started", "success"]
        assert repo.statuses("<msg-2@example.com>") == []

    def test_lock_is_checked_before_each_cursor_write(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.state = _imap_state(7, 0)
        lock = MagicMock()
        _run(FakeAdapter(_imap_read([1, 2])), source_lock=lock)
        assert lock.check.call_count == len(repo.imap_saves) == 2


class TestDriveCursor:
    def test_saves_the_token_when_everything_was_handled(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        _run(FakeAdapter(_drive_read(["f1", "f2"]), source_type="gdrive"))
        assert repo.drive_saves == [("tok-new", "acct-1", False)]

    def test_reset_is_counted(self, repo: FakeRepo, extract: MagicMock) -> None:
        read = _drive_read(["f1"], reset="invalid page token")
        _run(FakeAdapter(read, source_type="gdrive"))
        assert repo.drive_saves == [("tok-new", "acct-1", True)]

    def test_held_back_keeps_the_old_token(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        read = _drive_read(["f1"], held_back=True)
        result = _run(FakeAdapter(read, source_type="gdrive"))
        assert result.processed == 1
        assert repo.drive_saves == []

    def test_bootstrap_held_back_saves_no_token_or_account(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        read = _drive_read([], held_back=True)
        _run(FakeAdapter(read, source_type="gdrive"))
        assert repo.drive_saves == []

    def test_locked_item_keeps_the_old_token(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        repo.locked = {"f2"}
        _run(FakeAdapter(_drive_read(["f1", "f2"]), source_type="gdrive"))
        assert repo.drive_saves == []

    def test_limit_keeps_the_old_token(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        _run(FakeAdapter(_drive_read(["f1", "f2"]), source_type="gdrive"), limit=1)
        assert repo.drive_saves == []

    def test_unsupported_file_is_skipped_and_passed(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        adapter = FakeAdapter(
            _drive_read(["f1", "z"], skips={"z"}), source_type="gdrive"
        )

        result = _run(adapter)

        assert adapter.fetched == ["f1"]
        assert repo.statuses("z") == ["skipped"]
        skip_row = next(k for k in repo.row_kwargs if k["source_id"] == "z")
        assert "unsupported file type" in skip_row["error_message"]
        assert result.skipped == 1
        assert repo.drive_saves == [("tok-new", "acct-1", False)]


class TestSourceErrors:
    def test_list_error_is_recorded_not_raised(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        adapter = FakeAdapter(_imap_read([]), list_error=ConnectionError("refused"))

        result = _run(adapter)

        assert result.source_error == "read failed: refused"
        assert repo.polls == ["refused"]
        assert adapter.closed


class TestIngestResult:
    def test_defaults(self) -> None:
        result = IngestResult()
        assert result.processed == 0
        assert result.skipped == 0
        assert result.failed == 0
        assert result.receipts == []
        assert result.source_error is None


def _paid_extraction(confidence: float = 0.95) -> ExtractionResult:
    return ExtractionResult(
        metadata=ReceiptMetadata(
            vendor="Acme Parking (U.S.), LLC",
            amount=Decimal("50.00"),
            paid_amount=Decimal("30.00"),
            currency="USD",
            date=date(2025, 6, 15),
            description="Parking; 20.00 paid with a gift card",
            confidence=confidence,
            field_confidence=FieldConfidence(
                vendor=0.95, amount=0.9, paid_amount=0.7, date=0.99
            ),
        ),
        input_tokens=150,
        output_tokens=50,
        cache_read_tokens=0,
        requests=1,
    )


class TestPaidAmountPassThrough:
    """The pipeline stores the paid amount and per-field confidence."""

    def test_success_writes_paid_amount(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.return_value = _paid_extraction()
        store = _store()
        run_ingest(
            conn=MagicMock(),
            adapter=FakeAdapter(_imap_read([1])),  # type: ignore[arg-type]
            store=store,
            source_name="personal-email",
            source_type="imap",
            llm_model="test-model",
        )

        (receipt_kwargs,) = repo.receipt_kwargs
        assert receipt_kwargs["amount"] == Decimal("50.00")
        assert receipt_kwargs["paid_amount"] == Decimal("30.00")
        assert receipt_kwargs["field_confidence"] == {
            "vendor": 0.95,
            "amount": 0.9,
            "paid_amount": 0.7,
            "date": 0.99,
        }
        success = next(k for k in repo.row_kwargs if k["status"] == "success")
        assert success["amount"] == Decimal("50.00")
        assert success["paid_amount"] == Decimal("30.00")
        # The file name keeps the total; naming belongs to the evidence store.
        put_kwargs = store.put.call_args.kwargs
        assert put_kwargs["name"].endswith("__50.00.pdf")
        assert put_kwargs["purpose"] == "rendition"
        assert put_kwargs["media_type"] == "application/pdf"
        assert store.put.call_args.args == (b"%PDF",)
        # The receipt's pdf_path is the reference's relative path.
        assert (
            receipt_kwargs["pdf_path"]
            == "renditions/2025/06/2025-06-15__amazon__42.99.pdf"
        )

    def test_skip_logs_paid_amount(self, repo: FakeRepo, extract: MagicMock) -> None:
        extract.return_value = _paid_extraction(confidence=0.3)
        result = _run(FakeAdapter(_imap_read([1])))

        assert result.skipped == 1
        skipped = next(k for k in repo.row_kwargs if k["status"] == "skipped")
        assert skipped["paid_amount"] == Decimal("30.00")


# ---------------------------------------------------------------------------
# Extraction history (receipt data-model design, §3)
# ---------------------------------------------------------------------------


class TestIngestRecordsItsExtraction:
    def test_success_writes_an_ingest_extraction(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        from receipt_index.extraction import EXTRACTION_PROMPT_VERSION

        _run(FakeAdapter(_imap_read([1])), llm_model="claude-test")

        (row,) = repo.extractions
        assert row["receipt_id"] == _RECEIPT_ROW["id"]
        assert row["origin"] == "ingest"
        assert row["input_kind"] == "source"
        assert row["prompt_version"] == EXTRACTION_PROMPT_VERSION
        assert row["llm_model"] == "claude-test"
        assert row["output"]["vendor"] == "Amazon"
        assert row["output"]["amount"] == "42.99"
        assert "receipt_document" in row["output"]
        assert (row["llm_input_tokens"], row["llm_output_tokens"]) == (150, 50)
        assert row["replaced"] == {}

    def test_applied_fields_are_what_ingest_wrote(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        _run(FakeAdapter(_imap_read([1])))
        applied = repo.extractions[0]["applied_fields"]
        assert set(applied) == {
            "vendor",
            "amount",
            "currency",
            "receipt_date",
            "description",
            "confidence",
            "field_confidence.vendor",
            "field_confidence.amount",
            "field_confidence.paid_amount",
            "field_confidence.date",
            "kind",
        }  # paid_amount is None in this extraction, so it wasn't written

    def test_order_number_and_payment_are_stored(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        result = _extraction()
        result.metadata = result.metadata.model_copy(
            update={
                "order_number": "A-100",
                "payment": Payment(method="card", last4="1234", payer="Jane Doe"),
                "field_confidence": _FIELD_CONFIDENCE.model_copy(
                    update={"order_number": 0.9, "payment": 0.8}
                ),
            }
        )
        extract.return_value = result
        _run(FakeAdapter(_imap_read([1])))
        kwargs = repo.receipt_kwargs[0]
        assert kwargs["order_number"] == "A-100"
        assert kwargs["payment_method"] == "card"
        assert kwargs["payment_last4"] == "1234"
        assert kwargs["payer"] == "Jane Doe"
        assert kwargs["field_confidence"]["payment"] == 0.8
        applied = set(repo.extractions[0]["applied_fields"])
        assert {
            "order_number",
            "payment_method",
            "payment_last4",
            "payer",
            "field_confidence.order_number",
            "field_confidence.payment",
        } <= applied

    def test_kind_is_stored(self, repo: FakeRepo, extract: MagicMock) -> None:
        result = _extraction()
        result.metadata = result.metadata.model_copy(update={"kind": "refund"})
        extract.return_value = result
        _run(FakeAdapter(_imap_read([1])))
        assert repo.receipt_kwargs[0]["kind"] == "refund"

    def test_linking_rule_runs_before_the_commit(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        # The refund link and its event commit with the receipt, its
        # extraction row and the success row, or not at all.
        _run(FakeAdapter(_imap_read([1])))
        linked = [r.source_id for r in repo.linked]
        assert linked == [repo.receipt_kwargs[0]["source_id"]]
        link_at = repo.writes.index("link_rule")
        assert link_at > repo.writes.index("receipt")
        assert repo.writes.index("ingest_log:success") > link_at

    def test_receipt_and_extraction_commit_with_the_success_row(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        _run(FakeAdapter(_imap_read([1])))
        assert repo.writes == [
            "ingest_log:started",
            "receipt",
            "provenance",
            "rendition",
            "link_rule",
            "extraction",
            "ingest_log:success",
        ]
        # insert_receipt leaves the commit to insert_ingest_log.
        assert repo.receipt_kwargs[0]["commit"] is False

    def test_success_records_provenance_and_rendition(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        _run(FakeAdapter(_imap_read([1])))
        (prov,) = repo.provenance
        receipt_id = _RECEIPT_ROW["id"]
        assert prov["source_id"] == repo.receipt_kwargs[0]["source_id"]
        assert prov["receipt_id"] == receipt_id
        assert prov["source_name"] == repo.row_kwargs[0]["source_name"]
        assert prov["received_at"] is not None
        (rendition,) = repo.renditions
        assert rendition["receipt_id"] == receipt_id
        assert rendition["ref"].startswith("local:")
        assert len(rendition["sha256"]) == 32
        assert rendition["media_type"] == "application/pdf"

    def test_skip_records_no_provenance(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.return_value = _extraction(confidence=0.2)
        _run(FakeAdapter(_imap_read([1])))
        assert repo.provenance == []
        assert repo.renditions == []

    def test_a_failed_evidence_write_fails_the_item(
        self, repo: FakeRepo, extract: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pipeline, "record_rendition", MagicMock(side_effect=RuntimeError("db"))
        )
        _run(FakeAdapter(_imap_read([1])))
        assert "ingest_log:success" not in repo.writes
        assert repo.writes[-1] == "ingest_log:failed"

    def test_skip_writes_no_extraction(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.return_value = _extraction(confidence=0.2)
        _run(FakeAdapter(_imap_read([1])))
        assert repo.extractions == []

    def test_llm_model_is_required(self) -> None:
        with pytest.raises(TypeError, match="llm_model"):
            run_ingest(  # type: ignore[call-arg]
                conn=MagicMock(),
                adapter=FakeAdapter(_imap_read([1])),  # type: ignore[arg-type]
                store=_store(),
                source_name="personal-email",
                source_type="imap",
            )


# ---------------------------------------------------------------------------
# Failure classes and the retry scheduler
# ---------------------------------------------------------------------------


_M = "claude-test"


def _failed_rows(repo: FakeRepo) -> list[dict[str, Any]]:
    return [k for k in repo.row_kwargs if k["status"] == "failed"]


class TestFailureClasses:
    def test_content_failure_uses_an_attempt(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.side_effect = UnexpectedModelBehavior("output validation failed")
        result = _run(FakeAdapter(_imap_read([1, 2])))

        assert result.failed == 2
        assert result.source_error is None
        row = _failed_rows(repo)[0]
        assert row["error_class"] == "content"
        assert (row["attempt"], row["counts_attempt"]) == (1, True)
        assert row["next_retry_at"] is not None

    def test_refusal_is_a_content_failure(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.side_effect = ContentFilterError("refusal")
        result = _run(FakeAdapter(_imap_read([1, 2])))
        assert result.failed == 2
        assert {r["error_class"] for r in _failed_rows(repo)} == {"content"}

    @pytest.mark.parametrize(
        ("exc", "cls"),
        [
            (ModelOverloadedError(_M, "overloaded"), "transient"),
            (ModelAuthenticationError(_M, "invalid x-api-key"), "config"),
        ],
    )
    def test_transient_or_config_failure_stops_the_source(
        self, repo: FakeRepo, extract: MagicMock, exc: Exception, cls: str
    ) -> None:
        extract.side_effect = exc
        adapter = FakeAdapter(_imap_read([1, 2, 3]))
        result = _run(adapter)

        assert result.failed == 1
        assert adapter.fetched == ["<msg-1@example.com>"]
        assert result.source_error is not None
        assert cls in result.source_error
        (row,) = _failed_rows(repo)
        assert row["error_class"] == cls
        assert row["counts_attempt"] is False
        assert row["next_retry_at"] is not None
        # The failed item has its rows, so the cursor passes it; no further.
        assert repo.imap_saves[-1] == (7, 1, False)

    def test_failure_after_the_store_write_records_the_file(
        self, repo: FakeRepo, extract: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pipeline, "insert_receipt", MagicMock(side_effect=RuntimeError("db"))
        )
        _run(FakeAdapter(_imap_read([1])))
        (row,) = _failed_rows(repo)
        assert row["stored_ref"] == (
            "local:renditions/2025/06/2025-06-15__amazon__42.99.pdf"
        )

    def test_failure_before_the_store_write_records_no_file(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.side_effect = RuntimeError("LLM error")
        _run(FakeAdapter(_imap_read([1])))
        assert _failed_rows(repo)[0]["stored_ref"] is None


def _log(source_id: str, status: str, minutes_ago: int = 60, **kw: Any) -> Any:
    from uuid import uuid4

    from receipt_index.models import IngestLogEntry

    return IngestLogEntry.model_validate(
        {
            "id": uuid4(),
            "source_id": source_id,
            "source_type": "imap",
            "source_name": "personal-email",
            "status": status,
            "receipt_id": None,
            "vendor": None,
            "amount": None,
            "email_subject": "Receipt",
            "email_sender": "shop@example.com",
            "email_date": None,
            "error_message": "boom",
            "created_at": datetime.now(tz=UTC) - timedelta(minutes=minutes_ago),
            **kw,
        }
    )


class FetchByIdAdapter(FakeAdapter):
    def __init__(self, **kw: Any) -> None:
        super().__init__(_imap_read([]), **kw)

    def fetch_by_id(self, source_id: str) -> RawReceipt | None:
        uid = int(source_id.split("-")[1].split("@")[0])
        return self.fetch(_imap_ref(uid))


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch, repo: FakeRepo) -> dict[str, list[Any]]:
    """Each item's ingest_log rows, newest first, for the scheduler."""
    rows: dict[str, list[Any]] = {}
    monkeypatch.setattr(
        pipeline,
        "get_item_history",
        lambda conn, source_id, limit=100: rows.get(source_id, []),
    )
    monkeypatch.setattr(
        pipeline, "has_receipt", lambda conn, source_id: source_id in repo.receipts
    )
    monkeypatch.setattr(pipeline, "orphaned_refs", lambda conn, ids: {})
    return rows


def _retry(
    due: list[Any], adapter: FetchByIdAdapter, **kw: Any
) -> pipeline.RetryResult:
    from contextlib import ExitStack

    from receipt_index.config import ImapSourceConfig
    from receipt_index.fetching import SourcePool

    config = ImapSourceConfig(
        name="personal-email",
        type="imap",
        host="mail.example.com",
        username="user@example.com",
        password="x",  # pragma: allowlist secret
        folder="INBOX",
    )
    with ExitStack() as stack:
        pool = SourcePool([config], lambda _c: adapter, stack)  # type: ignore[arg-type,return-value]
        return pipeline.run_due_retries(
            MagicMock(),
            due,
            pool=pool,
            store=_store(),
            agents={"email": MagicMock(), "document": MagicMock()},
            llm_model="test-model",
            **kw,
        )


class TestRetryScheduler:
    def test_due_failure_is_retried_as_the_next_attempt(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "failed", attempt=1, next_retry_at=_past())]
        result = _retry(history[sid][:1], FetchByIdAdapter())

        assert result.processed == 1
        assert repo.statuses(sid) == ["started", "success"]
        assert [k["attempt"] for k in repo.row_kwargs] == [2, 2]

    def test_queued_item_starts_again_at_one(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [
            _log(sid, "queued", 1, attempt=0, counts_attempt=False),
            _log(sid, "failed", 90, attempt=3),
        ]
        _retry(history[sid][:1], FetchByIdAdapter())
        assert [k["attempt"] for k in repo.row_kwargs] == [1, 1]

    def test_item_no_longer_due_is_left_alone(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        listed = _log(sid, "failed", next_retry_at=_past())
        # Someone else finished it between selection and the lock.
        history[sid] = [_log(sid, "success", 0), listed]
        adapter = FetchByIdAdapter()
        result = _retry([listed], adapter)
        assert repo.rows == []
        assert adapter.fetched == []
        assert result.processed == result.failed == 0

    def test_skipped_item_never_becomes_an_automatic_retry(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        listed = _log(sid, "failed", next_retry_at=_past())
        history[sid] = [_log(sid, "skipped", 0), listed]
        _retry([listed], FetchByIdAdapter())
        assert repo.rows == []

    def test_locked_item_is_skipped(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "failed", next_retry_at=_past())]
        repo.locked = {sid}
        adapter = FetchByIdAdapter()
        _retry(history[sid], adapter)
        assert repo.rows == []
        assert adapter.fetched == []

    def test_dead_started_row_uses_up_an_attempt(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "started", attempt=2)]
        adapter = FetchByIdAdapter()
        result = _retry(history[sid], adapter)

        assert adapter.fetched == []
        assert result.failed == 1
        (row,) = _failed_rows(repo)
        assert (row["attempt"], row["counts_attempt"]) == (2, True)
        assert row["next_retry_at"] is not None
        assert "mid-item" in row["error_message"]

    def test_unreachable_source_writes_nothing_and_stops(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        ids = ["<msg-1@example.com>", "<msg-2@example.com>"]
        for sid in ids:
            history[sid] = [_log(sid, "failed", next_retry_at=_past())]
        adapter = FetchByIdAdapter(unavailable_from=ids[0])
        result = _retry([history[s][0] for s in ids], adapter)

        assert repo.rows == []  # no started row: no attempt used up
        assert adapter.fetched == [ids[0]]
        assert result.stopped is not None

    def test_transient_failure_stops_the_pass(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        extract.side_effect = ModelOverloadedError(_M, "overloaded")
        ids = ["<msg-1@example.com>", "<msg-2@example.com>"]
        for sid in ids:
            history[sid] = [_log(sid, "failed", attempt=1, next_retry_at=_past())]
        adapter = FetchByIdAdapter()
        result = _retry([history[s][0] for s in ids], adapter)

        assert adapter.fetched == [ids[0]]
        assert result.failed == 1
        assert result.stopped is not None
        (row,) = _failed_rows(repo)
        assert (row["attempt"], row["counts_attempt"]) == (2, False)

    def test_gone_item_is_a_counted_failure(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "failed", attempt=2, next_retry_at=_past())]
        _retry(history[sid], FetchByIdAdapter(gone={sid}))
        (row,) = _failed_rows(repo)
        assert (row["attempt"], row["counts_attempt"], row["error_class"]) == (
            3,
            True,
            "content",
        )
        assert row["next_retry_at"] is None  # attempt 3 of 3: terminal


class TestRetrySourceName:
    """A row that names no source keeps NULL, so later passes still find it."""

    def test_gone_legacy_item_keeps_no_source_name(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "queued", attempt=0, source_name=None)]
        _retry(history[sid], FetchByIdAdapter(gone={sid}))
        assert [k["source_name"] for k in repo.row_kwargs] == [None, None]

    def test_dead_started_legacy_item_keeps_no_source_name(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "started", source_name=None)]
        _retry(history[sid], FetchByIdAdapter())
        assert [k["source_name"] for k in repo.row_kwargs] == [None]

    def test_found_legacy_item_records_the_source_that_had_it(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "queued", attempt=0, source_name=None)]
        _retry(history[sid], FetchByIdAdapter())
        assert [k["source_name"] for k in repo.row_kwargs] == [
            "personal-email",
            "personal-email",
        ]


class TestRetryOtherSourceDown:
    def test_another_source_down_skips_only_that_item(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        """Two IMAP sources: "scans" is down, the pass runs for "personal-email".

        An item that names no source is looked for in "scans" first and left
        for later; the next item, from the healthy own source, still runs.
        """
        from contextlib import ExitStack

        from receipt_index.config import ImapSourceConfig
        from receipt_index.fetching import SourcePool

        legacy, own = "<msg-1@example.com>", "<msg-2@example.com>"
        history[legacy] = [_log(legacy, "queued", attempt=0, source_name=None)]
        history[own] = [_log(own, "failed", next_retry_at=_past())]
        down = FetchByIdAdapter(unavailable_from=legacy)
        healthy = FetchByIdAdapter()

        def config(name: str) -> ImapSourceConfig:
            return ImapSourceConfig(
                name=name,
                type="imap",
                host="mail.example.com",
                username="user@example.com",
                password="x",  # pragma: allowlist secret
                folder="INBOX",
            )

        adapters = {"scans": down, "personal-email": healthy}
        with ExitStack() as stack:
            pool = SourcePool(
                [config("scans"), config("personal-email")],
                lambda c: adapters[c.name],  # type: ignore[arg-type,return-value]
                stack,
            )
            result = pipeline.run_due_retries(
                MagicMock(),
                [history[legacy][0], history[own][0]],
                pool=pool,
                store=_store(),
                agents={"email": MagicMock(), "document": MagicMock()},
                llm_model="test-model",
                own_source="personal-email",
            )

        assert result.stopped is None
        assert result.processed == 1
        assert repo.statuses(legacy) == []  # left for a later run, no attempt used
        assert repo.statuses(own) == ["started", "success"]
        assert down.fetched == [legacy]

    def test_own_source_down_stops_the_pass(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "failed", next_retry_at=_past())]
        adapter = FetchByIdAdapter(unavailable_from=sid)
        result = _retry(history[sid], adapter, own_source="personal-email")
        assert result.stopped is not None


class TestRetryOrphans:
    def test_success_over_an_orphan_is_logged(
        self,
        repo: FakeRepo,
        extract: MagicMock,
        history: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "failed", next_retry_at=_past())]
        old = "local:renditions/2025/06/2025-06-15__amazon__42.99.pdf"
        monkeypatch.setattr(pipeline, "orphaned_refs", lambda conn, ids: {sid: [old]})
        with caplog.at_level(logging.WARNING):
            result = _retry(history[sid], FetchByIdAdapter())
        assert result.processed == 1
        assert old in caplog.text
        assert "failures --orphans" in caplog.text

    def test_no_warning_without_an_orphan(
        self,
        repo: FakeRepo,
        extract: MagicMock,
        history: dict[str, list[Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sid = "<msg-1@example.com>"
        history[sid] = [_log(sid, "failed", next_retry_at=_past())]
        with caplog.at_level(logging.WARNING):
            _retry(history[sid], FetchByIdAdapter())
        assert "orphans" not in caplog.text


def _past() -> datetime:
    return datetime.now(tz=UTC) - timedelta(minutes=1)


# ---------------------------------------------------------------------------
# Link-only receipts
# ---------------------------------------------------------------------------

_LINK = "https://receipts.example.com/r/abc123?token=s3cr3t-t0ken"


def _link_extraction(confidence: float, link: str | None = _LINK) -> ExtractionResult:
    result = _extraction(confidence=confidence)
    result.metadata = result.metadata.model_copy(update={"receipt_link": link})
    return result


class TestLinkOnly:
    def test_low_confidence_link_is_a_link_only_skip(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.return_value = _link_extraction(confidence=0.2)
        render = pipeline.render_rendition
        result = _run(FakeAdapter(_imap_read([1])))

        assert result.skipped == 1
        assert repo.statuses("<msg-1@example.com>") == ["started", "skipped"]
        row = next(k for k in repo.row_kwargs if k["status"] == "skipped")
        assert row["receipt_link"] == _LINK
        assert row["error_message"] == (
            "Skipped: link-only receipt (receipts.example.com)"
        )
        assert render.call_count == 0  # type: ignore[attr-defined]
        assert repo.receipts == set()

    def test_high_confidence_link_is_indexed_as_today(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.return_value = _link_extraction(confidence=0.95)
        result = _run(FakeAdapter(_imap_read([1])))

        assert result.processed == 1
        assert repo.statuses("<msg-1@example.com>") == ["started", "success"]
        assert all(k.get("receipt_link") is None for k in repo.row_kwargs)

    def test_low_confidence_without_link_is_a_plain_skip(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.return_value = _link_extraction(confidence=0.2, link=None)
        _run(FakeAdapter(_imap_read([1])))

        row = next(k for k in repo.row_kwargs if k["status"] == "skipped")
        assert row.get("receipt_link") is None
        assert row["error_message"].startswith("Skipped: amount=")

    def test_an_indexed_receipt_keeps_the_link_out_of_its_history(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        extract.return_value = _link_extraction(confidence=0.95)
        _run(FakeAdapter(_imap_read([1])))

        (extraction,) = repo.extractions
        assert "receipt_link" not in extraction["output"]
        assert "receipt_link" not in extraction["applied_fields"]

    def test_a_failed_extraction_logs_no_link(
        self,
        repo: FakeRepo,
        extract: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # pydantic-ai's output-validation error chains the model's output,
        # which can hold the link, into the traceback the warning logs.
        try:
            raise ValueError(
                f"Field required [input_value={{'receipt_link': '{_LINK}'}}]"
            )
        except ValueError as cause:
            failure = UnexpectedModelBehavior("Exceeded maximum output retries (1)")
            failure.__cause__ = cause
        extract.side_effect = failure
        with caplog.at_level(logging.DEBUG):
            _run(FakeAdapter(_imap_read([1])))

        assert "Exceeded maximum output retries" in caplog.text
        assert "receipts.example.com" in caplog.text
        assert "s3cr3t-t0ken" not in caplog.text
        row = next(k for k in repo.row_kwargs if k["status"] == "failed")
        assert "s3cr3t-t0ken" not in row["error_message"]

    def test_the_link_is_never_logged(
        self,
        repo: FakeRepo,
        extract: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        extract.return_value = _link_extraction(confidence=0.2)
        with caplog.at_level(logging.DEBUG):
            _run(FakeAdapter(_imap_read([1])))

        assert "receipts.example.com" in caplog.text
        assert "/r/abc123" not in caplog.text
        assert "s3cr3t-t0ken" not in caplog.text


class TestDiskFull:
    def test_disk_full_while_storing_stops_the_source_without_an_attempt(
        self, repo: FakeRepo, extract: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno

        monkeypatch.setattr(
            pipeline,
            "store_rendition",
            MagicMock(side_effect=OSError(errno.ENOSPC, "No space left on device")),
        )
        adapter = FakeAdapter(_imap_read([1, 2, 3]))
        result = _run(adapter)

        assert result.source_error is not None
        assert adapter.fetched == ["<msg-1@example.com>"]
        (row,) = _failed_rows(repo)
        assert row["error_class"] == "transient"
        assert row["counts_attempt"] is False


class StopAfter:
    """A RunControl that asks to stop once ``n`` items have run."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.ran: list[str] = []

    def should_stop(self) -> bool:
        return len(self.ran) >= self.n

    @contextmanager
    def running(self, source_id: str) -> Iterator[None]:
        yield
        self.ran.append(source_id)


class TestRunControl:
    def test_a_stop_ends_the_loop_between_items(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        control = StopAfter(1)
        adapter = FakeAdapter(_imap_read([1, 2, 3]))
        result = _run(adapter, control=control)

        assert control.ran == ["<msg-1@example.com>"]
        assert adapter.fetched == ["<msg-1@example.com>"]
        assert result.processed == 1
        # Every item that started has its outcome; the cursor stops there.
        assert repo.statuses("<msg-1@example.com>") == ["started", "success"]
        assert repo.statuses("<msg-2@example.com>") == []
        assert repo.imap_saves[-1] == (7, 1, False)

    def test_without_a_control_every_item_runs(
        self, repo: FakeRepo, extract: MagicMock
    ) -> None:
        result = _run(FakeAdapter(_imap_read([1, 2, 3])))
        assert result.processed == 3

    def test_a_stop_ends_the_retry_pass_between_items(
        self, repo: FakeRepo, extract: MagicMock, history: dict[str, list[Any]]
    ) -> None:
        due = []
        for n in (1, 2):
            sid = f"<msg-{n}@example.com>"
            history[sid] = [_log(sid, "failed", attempt=1, next_retry_at=_past())]
            due.append(history[sid][0])
        control = StopAfter(1)
        result = _retry(due, FetchByIdAdapter(), control=control)

        assert control.ran == ["<msg-1@example.com>"]
        assert result.processed == 1
        assert repo.statuses("<msg-2@example.com>") == []
