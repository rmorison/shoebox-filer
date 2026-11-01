"""``backfill``: the full mode and ``--originals-only``.

The loops run against a fake adapter and patched repository calls; the
adapters' date-range listings run against the fake IMAP server and Drive
service the adapter tests use. The end-to-end runs are integration tests
(``tests/integration/test_backfill.py``).
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from imapclient.exceptions import IMAPClientError

from receipt_index import backfill
from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.adapters.gdrive import GdriveAdapter
from receipt_index.adapters.imap import (
    ImapAdapter,
    _is_session_refusal,
    _range_criteria,
    can_search_by_id,
)
from receipt_index.fetching import Fetched, SourceDownError, SourcePool
from receipt_index.models import ItemRef, Provenance, RawReceipt
from receipt_index.pipeline import ItemOutcome
from tests.unit.test_fetch_by_id import SearchingIMAPClient
from tests.unit.test_gdrive_adapter import FakeDrive, _make_gdrive_config
from tests.unit.test_imap_adapter import FakeIMAPClient, _make_simple_email

if TYPE_CHECKING:
    from collections.abc import Iterator

    from receipt_index.config import ImapSourceConfig

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _provenance(
    source_id: str,
    *,
    status: str = "pending",
    source_type: str = "imap",
    received_at: datetime = _NOW,
) -> Provenance:
    return Provenance.model_validate(
        {
            "id": uuid4(),
            "receipt_id": uuid4(),
            "is_primary": True,
            "source_type": source_type,
            "source_name": "mail",
            "source_id": source_id,
            "received_at": received_at,
            "original_status": status,
            "match_method": "first",
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    )


def _raw(source_id: str) -> RawReceipt:
    return RawReceipt(
        source_id=source_id,
        source_name="mail",
        source_type="imap",
        date=_NOW,
        subject="s",
        sender="f",
        raw_bytes=b"raw",
    )


class FakeAdapter:
    """A source that lists fixed refs and fetches each by UID."""

    source_type = "imap"
    folder = "INBOX"

    def __init__(self, refs: list[ItemRef], *, list_error: Exception | None = None):
        self.refs = refs
        self.list_error = list_error
        self.fetched: list[str] = []
        self.ranges: list[tuple[date | None, date | None]] = []

    def list_range(self, since: date | None, until: date | None) -> list[ItemRef]:
        self.ranges.append((since, until))
        if self.list_error is not None:
            raise self.list_error
        return self.refs

    def fetch(self, ref: ItemRef) -> RawReceipt | None:
        self.fetched.append(ref.source_id)
        return _raw(ref.source_id)

    def list_new(self, state: Any) -> Any:
        raise AssertionError("backfill never reads incrementally")

    def fetch_by_id(self, source_id: str) -> RawReceipt | None:
        raise AssertionError("not used")

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


def _ref(n: int) -> ItemRef:
    return ItemRef(source_id=f"<m{n}@example.com>", source_type="imap", imap_uid=n)


class Harness:
    """Patches the repository calls ``backfill`` makes, and records them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.processed: set[str] = set()
        self.pending: list[Provenance] = []
        self.current: dict[UUID, Provenance] = {}
        self.locked: set[str] = set()
        self.handled: list[str] = []
        self.outcomes: dict[str, ItemOutcome] = {}
        self.attached: list[UUID] = []
        self.marked: list[tuple[UUID, str]] = []
        self.stored: list[tuple[str, datetime]] = []
        self.conn = MagicMock()

        monkeypatch.setattr(
            backfill, "get_processed_source_ids", lambda conn: set(self.processed)
        )
        monkeypatch.setattr(
            backfill,
            "provenance_missing_originals",
            lambda conn, name, since=None, until=None: list(self.pending),
        )
        monkeypatch.setattr(
            backfill,
            "get_provenance",
            lambda conn, pid: self.current.get(pid),
        )
        monkeypatch.setattr(
            backfill, "latest_receipt_document", lambda conn, receipt_id: None
        )

        @contextmanager
        def lock(conn: Any, source_id: str) -> Iterator[bool]:
            yield source_id not in self.locked

        monkeypatch.setattr(backfill, "item_lock", lock)

        def handle(conn: Any, adapter: Any, ref: ItemRef, **kw: Any) -> ItemOutcome:
            self.handled.append(ref.source_id)
            return self.outcomes.get(ref.source_id, ItemOutcome("success", MagicMock()))

        monkeypatch.setattr(backfill, "handle_item", handle)

        def store(conn: Any, store: Any, raw: RawReceipt, **kw: Any) -> object:
            self.stored.append((raw.source_id, kw["received_at"]))
            return object()

        monkeypatch.setattr(backfill, "store_email_original", store)

        def attach(conn: Any, pid: UUID, email: Any, **kw: Any) -> bool:
            self.attached.append(pid)
            return True

        monkeypatch.setattr(backfill, "attach_email_original", attach)

        def mark(conn: Any, pid: UUID, reason: str) -> bool:
            self.marked.append((pid, reason))
            return True

        monkeypatch.setattr(backfill, "mark_original_unavailable", mark)

    def add_pending(self, *rows: Provenance) -> None:
        for row in rows:
            self.pending.append(row)
            self.current[row.id] = row

    def run(self, adapter: FakeAdapter, **kw: Any) -> backfill.BackfillResult:
        return backfill.run_backfill(
            self.conn,
            adapter,  # type: ignore[arg-type]
            store=MagicMock(),
            source_name="mail",
            source_type="imap",
            llm_model="m",
            agent=None,
            **kw,
        )

    def originals(self, fetched: Any, **kw: Any) -> backfill.OriginalsResult:
        return backfill.run_originals_only(
            self.conn, MagicMock(), store=MagicMock(), source_name="mail", **kw
        )


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(monkeypatch)


# ---------------------------------------------------------------------------
# Full mode
# ---------------------------------------------------------------------------


class TestRunBackfill:
    def test_unseen_items_are_handled_and_seen_ones_skipped(self, h: Harness) -> None:
        h.processed = {"<m1@example.com>"}
        result = h.run(FakeAdapter([_ref(1), _ref(2), _ref(3)]))
        assert h.handled == ["<m2@example.com>", "<m3@example.com>"]
        assert (result.listed, result.unseen, result.processed) == (3, 2, 2)

    def test_passes_the_range_to_the_listing(self, h: Harness) -> None:
        adapter = FakeAdapter([])
        h.run(adapter, since=date(2025, 1, 1), until=date(2025, 1, 31))
        assert adapter.ranges == [(date(2025, 1, 1), date(2025, 1, 31))]

    def test_seen_item_with_a_pending_original_gets_it(self, h: Harness) -> None:
        row = _provenance(
            "<m1@example.com>", received_at=datetime(2024, 3, 2, tzinfo=UTC)
        )
        h.processed = {row.source_id}
        h.add_pending(row)
        adapter = FakeAdapter([_ref(1)])
        result = h.run(adapter)
        assert h.handled == []
        assert adapter.fetched == [row.source_id]
        # Dated by the row's received_at, so a re-run reuses the folder.
        assert h.stored == [(row.source_id, row.received_at)]
        assert h.attached == [row.id]
        assert (result.originals_missing, result.originals_stored) == (1, 1)

    def test_seen_item_whose_original_was_stored_meanwhile_is_left(
        self, h: Harness
    ) -> None:
        row = _provenance("<m1@example.com>")
        h.processed = {row.source_id}
        h.add_pending(row)
        h.current[row.id] = _provenance(row.source_id, status="stored")
        adapter = FakeAdapter([_ref(1)])
        result = h.run(adapter)
        assert adapter.fetched == []
        assert result.originals_stored == 0

    def test_limit_caps_unseen_items(self, h: Harness) -> None:
        result = h.run(FakeAdapter([_ref(1), _ref(2), _ref(3)]), limit=2)
        assert h.handled == ["<m1@example.com>", "<m2@example.com>"]
        assert result.unseen == 2

    def test_locked_item_is_counted_and_left(self, h: Harness) -> None:
        h.outcomes["<m1@example.com>"] = ItemOutcome("locked")
        result = h.run(FakeAdapter([_ref(1), _ref(2)]))
        assert (result.locked, result.processed) == (1, 1)

    def test_transient_failure_stops_the_run(self, h: Harness) -> None:
        h.outcomes["<m1@example.com>"] = ItemOutcome("failed", error_class="transient")
        result = h.run(FakeAdapter([_ref(1), _ref(2)]))
        assert h.handled == ["<m1@example.com>"]
        assert result.failed == 1
        assert result.source_error is not None

    def test_source_unavailable_stops_the_run(
        self, h: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def down(*a: Any, **kw: Any) -> ItemOutcome:
            raise SourceUnavailableError("gone away")

        monkeypatch.setattr(backfill, "handle_item", down)
        result = h.run(FakeAdapter([_ref(1)]))
        assert result.source_error == "gone away"

    def test_listing_failure_is_a_source_error(self, h: Harness) -> None:
        result = h.run(FakeAdapter([], list_error=OSError("refused")))
        assert result.source_error is not None
        assert "refused" in result.source_error

    def test_dry_run_counts_what_a_real_run_handles_and_writes_nothing(
        self, h: Harness
    ) -> None:
        row = _provenance("<m1@example.com>")
        h.processed = {row.source_id, "<m2@example.com>"}
        h.add_pending(row)
        refs = [_ref(1), _ref(2), _ref(3), _ref(4), _ref(5)]
        dry = h.run(FakeAdapter(refs), dry_run=True, limit=3)
        assert h.handled == [] and h.stored == [] and h.attached == []
        # --limit counts the fill and the unseen items together.
        assert (dry.unseen, dry.originals_missing) == (2, 1)
        real = h.run(FakeAdapter(refs), limit=3)
        assert len(h.handled) == dry.unseen == real.unseen
        assert real.originals_stored == dry.originals_missing

    def test_never_moves_a_cursor(
        self, h: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Backfill imports none of the cursor writers; if one is ever wired
        # in through the pipeline module, this catches it.
        from receipt_index import pipeline

        for name in ("save_imap_cursor", "save_drive_cursor", "record_poll"):
            monkeypatch.setattr(
                pipeline, name, MagicMock(side_effect=AssertionError(name))
            )
            assert not hasattr(backfill, name)
        h.run(FakeAdapter([_ref(1)]))

    def test_drive_source_fills_no_originals(
        self, h: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _provenance("file-1", source_type="gdrive")
        h.processed = {row.source_id}
        h.add_pending(row)
        ref = ItemRef(source_id="file-1", source_type="gdrive", drive_meta={})
        adapter = FakeAdapter([ref])
        result = backfill.run_backfill(
            h.conn,
            adapter,  # type: ignore[arg-type]
            store=MagicMock(),
            source_name="drive",
            source_type="gdrive",
            llm_model="m",
            agent=None,
        )
        assert adapter.fetched == []
        assert result.originals_missing == 0

    def test_seen_item_gone_between_listing_and_fetch_is_left_pending(
        self, h: Harness
    ) -> None:
        row = _provenance("<m1@example.com>")
        h.processed = {row.source_id}
        h.add_pending(row)

        class Gone(FakeAdapter):
            def fetch(self, ref: ItemRef) -> RawReceipt | None:
                return None

        result = h.run(Gone([_ref(1)]))
        assert h.marked == [] and h.attached == []
        assert (result.originals_stored, result.originals_failed) == (0, 1)


# ---------------------------------------------------------------------------
# --originals-only
# ---------------------------------------------------------------------------


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What the shared fetch path returns, by source ID (default: found)."""
    answers: dict[str, Any] = {}
    calls: list[str] = []

    def fetch_item(pool: Any, *, source_id: str, **kw: Any) -> Fetched:
        calls.append(source_id)
        answer = answers.get(source_id, "found")
        if isinstance(answer, BaseException):
            raise answer
        if answer == "found":
            return Fetched(_raw(source_id), "mail")
        return answer  # type: ignore[no-any-return]

    monkeypatch.setattr(backfill, "fetch_item", fetch_item)
    answers["__calls__"] = calls
    return answers


class TestRunOriginalsOnly:
    def test_store_failure_rolls_back_and_leaves_the_row_pending(
        self,
        h: Harness,
        fetched: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def broken(*a: Any, **kw: Any) -> object:
            raise OSError("disk full")

        monkeypatch.setattr(backfill, "store_email_original", broken)
        h.add_pending(_provenance("<a@example.com>"))
        result = h.originals(fetched)
        assert h.attached == [] and h.marked == []
        assert result.failed == 1
        h.conn.rollback.assert_called()

    def test_row_changed_under_the_update_counts_as_unchanged(
        self,
        h: Harness,
        fetched: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(backfill, "attach_email_original", lambda *a, **kw: False)
        monkeypatch.setattr(
            backfill, "mark_original_unavailable", lambda *a, **kw: False
        )
        a, b = _provenance("<a@example.com>"), _provenance("<b@example.com>")
        h.add_pending(a, b)
        fetched[b.source_id] = Fetched(None)
        result = h.originals(fetched)
        assert (result.stored, result.unavailable, result.unchanged) == (0, 0, 2)

    def test_found_item_is_stored_and_attached(
        self, h: Harness, fetched: dict[str, Any]
    ) -> None:
        row = _provenance(
            "<a@example.com>", received_at=datetime(2023, 5, 6, tzinfo=UTC)
        )
        h.add_pending(row)
        result = h.originals(fetched)
        assert h.stored == [(row.source_id, row.received_at)]
        assert h.attached == [row.id]
        assert result.stored == 1

    def test_missing_item_is_marked_unavailable_with_a_reason(
        self, h: Harness, fetched: dict[str, Any]
    ) -> None:
        row = _provenance("<a@example.com>")
        h.add_pending(row)
        fetched[row.source_id] = Fetched(None)
        result = h.originals(fetched)
        assert result.unavailable == 1
        assert h.marked[0][0] == row.id
        assert "Message-ID" in h.marked[0][1]

    def test_refused_lookup_leaves_the_row_pending(
        self, h: Harness, fetched: dict[str, Any]
    ) -> None:
        row = _provenance("<a@example.com>")
        h.add_pending(row)
        fetched[row.source_id] = Fetched(None, item_error=IMAPClientError("NO"))
        result = h.originals(fetched)
        assert h.marked == []
        assert result.failed == 1

    def test_source_down_stops_the_run_and_marks_nothing(
        self, h: Harness, fetched: dict[str, Any]
    ) -> None:
        a, b = _provenance("<a@example.com>"), _provenance("<b@example.com>")
        h.add_pending(a, b)
        fetched[a.source_id] = SourceDownError("source mail unavailable")
        result = h.originals(fetched)
        assert fetched["__calls__"] == [a.source_id]
        assert h.marked == [] and h.attached == []
        assert result.source_error == "source mail unavailable"

    def test_unsearchable_and_drive_rows_stay_pending(
        self, h: Harness, fetched: dict[str, Any]
    ) -> None:
        h.add_pending(
            _provenance("a" * 64),  # a fallback-hash ID
            _provenance("file-1", source_type="gdrive"),
        )
        result = h.originals(fetched)
        assert fetched["__calls__"] == []
        assert (result.unsearchable, result.drive_pending, result.to_fetch) == (1, 1, 0)

    def test_locked_row_is_skipped(self, h: Harness, fetched: dict[str, Any]) -> None:
        row = _provenance("<a@example.com>")
        h.add_pending(row)
        h.locked.add(row.source_id)
        result = h.originals(fetched)
        assert fetched["__calls__"] == []
        assert result.locked == 1

    def test_row_stored_between_listing_and_locking_is_left(
        self, h: Harness, fetched: dict[str, Any]
    ) -> None:
        row = _provenance("<a@example.com>")
        h.add_pending(row)
        h.current[row.id] = _provenance(row.source_id, status="stored")
        result = h.originals(fetched)
        assert fetched["__calls__"] == []
        assert result.unchanged == 1

    def test_limit_and_dry_run(self, h: Harness, fetched: dict[str, Any]) -> None:
        h.add_pending(*(_provenance(f"<{n}@example.com>") for n in range(3)))
        dry = h.originals(fetched, dry_run=True, limit=2)
        assert fetched["__calls__"] == [] and h.stored == []
        assert dry.to_fetch == 2
        real = h.originals(fetched, limit=2)
        assert real.stored == dry.to_fetch

    def test_makes_no_llm_call(
        self, h: Harness, fetched: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from receipt_index import extraction, pipeline

        boom = MagicMock(side_effect=AssertionError("LLM called"))
        monkeypatch.setattr(extraction, "extract_metadata", boom)
        monkeypatch.setattr(pipeline, "extract_metadata", boom)
        h.add_pending(_provenance("<a@example.com>"))
        assert h.originals(fetched).stored == 1
        boom.assert_not_called()


# ---------------------------------------------------------------------------
# Adapters: date-range listings
# ---------------------------------------------------------------------------


class RangeIMAPClient(FakeIMAPClient):
    """The fake server, answering date-range searches with every message."""

    def search(self, criteria: Any) -> list[int]:
        self.searches.append(criteria)
        return sorted(self.messages)


class TestImapListRange:
    def test_criteria(self) -> None:
        assert _range_criteria(None, None) == ["ALL"]
        # BEFORE excludes its own day, so --until gets the day after.
        assert _range_criteria(date(2025, 1, 1), date(2025, 1, 31)) == [
            "SINCE",
            date(2025, 1, 1),
            "BEFORE",
            date(2025, 2, 1),
        ]
        assert _range_criteria(None, date(2025, 12, 31)) == ["BEFORE", date(2026, 1, 1)]

    def test_lists_refs_with_their_folder_once_each(
        self, imap_config: ImapSourceConfig
    ) -> None:
        fake = RangeIMAPClient(
            {
                3: _make_simple_email(message_id="<a@example.com>"),
                4: _make_simple_email(message_id="<b@example.com>"),
                5: _make_simple_email(message_id="<a@example.com>"),
            }
        )
        adapter = ImapAdapter(imap_config)
        adapter._client = fake  # type: ignore[assignment]
        refs = adapter.list_range(date(2025, 1, 1), None)
        assert [(r.source_id, r.imap_uid, r.imap_folder) for r in refs] == [
            ("<a@example.com>", 3, "INBOX"),
            ("<b@example.com>", 4, "INBOX"),
        ]
        assert fake.searches == [["SINCE", date(2025, 1, 1)]]
        assert fake.selected == ("INBOX", True)

    def test_fetch_selects_the_refs_folder(self, imap_config: ImapSourceConfig) -> None:
        fake = RangeIMAPClient({3: _make_simple_email(message_id="<a@example.com>")})
        adapter = ImapAdapter(imap_config)
        adapter._client = fake  # type: ignore[assignment]
        (ref,) = adapter.list_range(None, None)
        fake.selected = ("Other", True)
        adapter._selected = "Other"
        raw = adapter.fetch(ref)
        assert raw is not None and raw.source_id == "<a@example.com>"
        assert fake.selected == ("INBOX", True)

    def test_fetch_refuses_a_folder_renumbered_since_listing(
        self, imap_config: ImapSourceConfig
    ) -> None:
        fake = RangeIMAPClient({3: _make_simple_email(message_id="<a@example.com>")})
        adapter = ImapAdapter(imap_config)
        adapter._client = fake  # type: ignore[assignment]
        (ref,) = adapter.list_range(None, None)
        adapter._selected = None
        fake.uidvalidity = 99
        with pytest.raises(SourceUnavailableError, match="UIDVALIDITY"):
            adapter.fetch(ref)

    def test_watched_folder_ref_reselects_after_another_folder(
        self, imap_config: ImapSourceConfig
    ) -> None:
        # A list_new ref names no folder: it is the watched folder's, even
        # after a lookup left another folder selected on the same session.
        fake = RangeIMAPClient({3: _make_simple_email(message_id="<a@example.com>")})
        adapter = ImapAdapter(imap_config)
        adapter._client = fake  # type: ignore[assignment]
        fake.selected = ("Archive", True)
        adapter._selected = "Archive"
        ref = ItemRef(source_id="<a@example.com>", source_type="imap", imap_uid=3)
        assert adapter.fetch(ref) is not None
        assert fake.selected == ("INBOX", True)

    def test_unreadable_ref_names_the_folder_it_was_listed_in(
        self, imap_config: ImapSourceConfig
    ) -> None:
        adapter = ImapAdapter(imap_config)
        ref = adapter._unreadable_ref(3, 7, "NO", "Archive")
        assert ref.source_id == "imap-uid:test-imap:Archive:7:3"

    def test_search_by_id(self) -> None:
        assert can_search_by_id("<a@example.com>")
        assert not can_search_by_id("a" * 64)
        assert not can_search_by_id("imap-uid:mail:INBOX:7:3")

    def test_gmail_throttle_is_a_session_refusal(self) -> None:
        exc = IMAPClientError(
            "FETCH command error: NO [ALERT] Account exceeded command or "
            "bandwidth limits."
        )
        assert _is_session_refusal(exc)


class RecordingDrive(FakeDrive):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.queries: list[str] = []

    def list(self, **kwargs: Any) -> Any:
        if "q" in kwargs:
            self.queries.append(kwargs["q"])
        return super().list(**kwargs)


class TestDriveListRange:
    def test_query_and_refs(self) -> None:
        old = "2025-01-10T00:00:00.000Z"
        drive = RecordingDrive(
            files=[
                {
                    "id": "f1",
                    "name": "a.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": old,
                },
                {
                    "id": "d1",
                    "name": "sub",
                    "mimeType": "application/vnd.google-apps.folder",
                    "modifiedTime": old,
                },
                {
                    "id": "f2",
                    "name": "b.zip",
                    "mimeType": "application/zip",
                    "modifiedTime": old,
                },
            ]
        )
        adapter = GdriveAdapter(_make_gdrive_config())
        adapter._service = drive
        refs = adapter.list_range(date(2025, 1, 1), date(2025, 1, 31))
        assert drive.queries == [
            "'folder_abc123' in parents and trashed = false"
            " and createdTime >= '2025-01-01T00:00:00Z'"
            " and createdTime < '2025-02-01T00:00:00Z'"
        ]
        assert [r.source_id for r in refs] == ["f1", "f2"]
        assert refs[0].skip_reason is None
        assert refs[1].skip_reason is not None  # recorded as skipped, as in ingest


# ---------------------------------------------------------------------------
# --originals-only through the real fetch path, against the IMAP fake
# ---------------------------------------------------------------------------


class BodylessIMAPClient(SearchingIMAPClient):
    """Answers a body FETCH with OK and no BODY[] item."""

    def fetch(self, uids: list[int], items: list[str]) -> dict[int, dict[bytes, Any]]:
        if items == ["BODY.PEEK[]"]:
            self.fetches.append((list(uids), list(items)))
            return {u: {b"SEQ": u} for u in uids}
        return super().fetch(uids, items)


def _pool_over(fake: FakeIMAPClient, imap_config: ImapSourceConfig) -> Any:
    def factory(config: Any) -> ImapAdapter:
        adapter = ImapAdapter(config)
        adapter._client = fake  # type: ignore[assignment]
        return adapter

    stack = ExitStack()
    return SourcePool([imap_config], factory, stack)  # type: ignore[arg-type]


class TestOriginalsOnlyAgainstTheImapFake:
    """No mocked fetch_item: the adapter decides found, gone or neither."""

    def _run(self, h: Harness, fake: FakeIMAPClient, imap_config: Any) -> Any:
        return backfill.run_originals_only(
            h.conn,
            _pool_over(fake, imap_config),
            store=MagicMock(),
            source_name=imap_config.name,
        )

    def _row(self, h: Harness, source_id: str, imap_config: Any) -> Provenance:
        row = _provenance(source_id).model_copy(
            update={"source_name": imap_config.name}
        )
        h.add_pending(row)
        return row

    def test_present_message_is_stored(
        self, h: Harness, imap_config: ImapSourceConfig
    ) -> None:
        row = self._row(h, "<a@example.com>", imap_config)
        fake = SearchingIMAPClient({3: _make_simple_email(message_id=row.source_id)})
        result = self._run(h, fake, imap_config)
        assert (result.stored, h.attached) == (1, [row.id])

    def test_zero_matches_is_unavailable_and_names_the_folder(
        self, h: Harness, imap_config: ImapSourceConfig
    ) -> None:
        row = self._row(h, "<gone@example.com>", imap_config)
        fake = SearchingIMAPClient(
            {3: _make_simple_email(message_id="<a@example.com>")}
        )
        result = self._run(h, fake, imap_config)
        assert result.unavailable == 1
        assert h.marked == [
            (row.id, "not found in INBOX by Message-ID (source test-imap)")
        ]

    def test_duplicate_message_id_is_ambiguous_and_stays_pending(
        self, h: Harness, imap_config: ImapSourceConfig
    ) -> None:
        row = self._row(h, "<dup@example.com>", imap_config)
        fake = SearchingIMAPClient(
            {
                3: _make_simple_email(message_id=row.source_id, subject="A"),
                4: _make_simple_email(message_id=row.source_id, subject="B"),
            }
        )
        result = self._run(h, fake, imap_config)
        assert h.marked == [] and h.attached == []
        assert (result.ambiguous, result.unavailable) == (1, 0)

    def test_empty_body_stays_pending(
        self, h: Harness, imap_config: ImapSourceConfig
    ) -> None:
        row = self._row(h, "<a@example.com>", imap_config)
        fake = BodylessIMAPClient({3: _make_simple_email(message_id=row.source_id)})
        result = self._run(h, fake, imap_config)
        assert h.marked == [] and h.attached == []
        assert (result.failed, result.unavailable) == (1, 0)

    def test_empty_header_answer_stays_pending(
        self, h: Harness, imap_config: ImapSourceConfig
    ) -> None:
        row = self._row(h, "<a@example.com>", imap_config)
        fake = SearchingIMAPClient({3: _make_simple_email(message_id=row.source_id)})
        fake.drop_headers.add(3)
        result = self._run(h, fake, imap_config)
        assert h.marked == [] and h.attached == []
        assert (result.failed, result.unavailable) == (1, 0)


class TestLinkedRows:
    def test_receipt_document_comes_from_the_rows_home_receipt(
        self,
        h: Harness,
        fetched: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Duplicate detection moved this row onto another receipt; its own
        # email was extracted on its home receipt.
        asked: list[UUID] = []

        def document(conn: Any, receipt_id: UUID) -> None:
            asked.append(receipt_id)

        monkeypatch.setattr(backfill, "latest_receipt_document", document)
        home = uuid4()
        row = _provenance("<a@example.com>").model_copy(
            update={"home_receipt_id": home}
        )
        h.add_pending(row)
        assert h.originals(fetched).stored == 1
        assert asked == [home]


class TestFullModeFills:
    def test_limit_caps_original_fills_too(self, h: Harness) -> None:
        rows = [_provenance(f"<m{n}@example.com>") for n in (1, 2, 3)]
        h.processed = {r.source_id for r in rows} | {"<m4@example.com>"}
        h.add_pending(*rows)
        refs = [_ref(1), _ref(2), _ref(3), _ref(5)]
        dry = h.run(FakeAdapter(refs), dry_run=True, limit=2)
        assert (dry.originals_missing, dry.unseen) == (2, 0)
        adapter = FakeAdapter(refs)
        result = h.run(adapter, limit=2)
        assert len(adapter.fetched) == 2
        assert (result.originals_stored, result.unseen) == (2, 0)
        assert h.handled == []

    def test_every_row_of_one_email_is_filled_from_one_fetch(self, h: Harness) -> None:
        first = _provenance("<m1@example.com>")
        second = _provenance("<m1@example.com>")
        h.processed = {first.source_id}
        h.add_pending(first, second)
        adapter = FakeAdapter([_ref(1)])
        result = h.run(adapter)
        assert adapter.fetched == ["<m1@example.com>"]
        assert h.attached == [first.id, second.id]
        assert result.originals_stored == 2


class TestSourceDownAtTheFirstLookup:
    def test_stops_cleanly_and_marks_nothing(
        self, h: Harness, imap_config: ImapSourceConfig
    ) -> None:
        def factory(config: Any) -> ImapAdapter:
            adapter = ImapAdapter(config)

            def refuse() -> Any:
                raise ConnectionError("Failed to connect to IMAP after 3 attempts")

            adapter._connect = refuse  # type: ignore[method-assign]
            return adapter

        a = _provenance("<a@example.com>").model_copy(
            update={"source_name": imap_config.name}
        )
        b = _provenance("<b@example.com>").model_copy(
            update={"source_name": imap_config.name}
        )
        h.add_pending(a, b)
        with ExitStack() as stack:
            pool = SourcePool([imap_config], factory, stack)
            result = backfill.run_originals_only(
                h.conn, pool, store=MagicMock(), source_name=imap_config.name
            )
        assert result.source_error is not None
        assert "unavailable" in result.source_error
        assert h.marked == [] and h.attached == []
        assert (result.stored, result.unavailable, result.failed) == (0, 0, 0)

    def test_the_reason_needs_no_connection(
        self, imap_config: ImapSourceConfig
    ) -> None:
        def factory(config: Any) -> ImapAdapter:
            raise AssertionError("the reason must not open the source")

        with ExitStack() as stack:
            pool = SourcePool([imap_config], factory, stack)
            reason = backfill._unavailable_reason(pool, imap_config.name)
        assert reason == "not found in INBOX by Message-ID (source test-imap)"
