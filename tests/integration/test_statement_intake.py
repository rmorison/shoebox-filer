"""Statement intake against Postgres, with stubbed models (plan U6).

Every statement is a synthetic PDF built by the test, and every model reply
is the test's own: no real statement, institution, name or amount.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from receipt_index import pipeline
from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.config import GdriveSourceConfig
from receipt_index.fetching import SourcePool
from receipt_index.models import ItemRef, RawReceipt, SourceRead
from receipt_index.repository import (
    count_ingest_failures,
    count_statement_failures,
    count_statements_by_source,
    get_item_history,
    insert_ingest_log,
    list_sync_state,
    orphaned_refs,
    select_due,
)
from receipt_index.retries import RetryPolicy, attempt_state
from receipt_index.statements import STATEMENT_SOURCE_TYPE
from tests.integration.test_statement_pipeline import (
    _ACCOUNT,
    _FIRST_PASS,
    _PAGE,
    _SOURCE,
    Replies,
    _one,
    _originals,
    _raw,
    _run,
    store,
)
from tests.pdfs import make_pdf

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg

    from receipt_index.store import LocalEvidenceStore

pytestmark = pytest.mark.integration

__all__ = ["store"]  # a fixture, imported for pytest

# A synthetic card number in a file name.
_NUMBER = "4929381766207319"
_NAME = f"Statement_{_NUMBER}.pdf"

_THREE_ACCOUNTS: dict[str, Any] = {
    **_FIRST_PASS,
    "accounts": [
        {
            "label": f"Account {n}",
            "account_type": "deposit",
            "first_page": 1,
            "last_page": 1,
        }
        for n in (1, 2, 3)
    ],
    "printed_account_count": 3,
}


class StopDuring(Replies):
    """Asks the process to stop during the given account call."""

    def __init__(self, stop_on_call: int | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stop_on_call = stop_on_call
        self.stopping = False

    def _model(self, kind: str) -> Any:
        model = super()._model(kind)
        inner = model.function

        def reply(messages: Any, info: Any) -> Any:
            response = inner(messages, info)  # type: ignore[misc]
            accounts = [c for c in self.calls if c != "first_pass"]
            if kind != "first_pass" and len(accounts) == self.stop_on_call:
                self.stopping = True
            return response

        model.function = reply
        return model


class Control:
    """serve's RunControl, as far as a statement uses it."""

    def __init__(self, replies: StopDuring) -> None:
        self.replies = replies
        self.steps = 0

    def should_stop(self) -> bool:
        return self.replies.stopping

    def running(self, source_id: str) -> Any:
        return nullcontext()

    def step(self, source_id: str) -> None:
        self.steps += 1


def _count(conn: psycopg.Connection[dict[str, Any]], table: str) -> int:
    return int(_one(conn, f"SELECT count(*) AS n FROM receipt.{table}")["n"])


class TestShutdownDuringAStatement:
    def test_a_stop_during_the_second_account_call_ends_it_uncounted(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        tmp_path: Path,
    ) -> None:
        raw = _raw(make_pdf(_PAGE))
        replies = StopDuring(2, first_pass=_THREE_ACCOUNTS)
        outcome = _run(
            pg_conn, raw, store, replies, policy=RetryPolicy(), control=Control(replies)
        )
        # The third account was never called: the item ended between calls.
        assert replies.calls == ["first_pass", "deposit", "deposit"]
        assert (outcome.status, outcome.error_class) == ("failed", "config")
        assert _count(pg_conn, "statements") == 0
        assert _count(pg_conn, "statement_revisions") == 0
        latest, *_ = get_item_history(pg_conn, raw.source_id)
        assert latest.status == "failed"
        assert latest.counts_attempt is False
        assert latest.next_retry_at is not None
        assert latest.error_message is not None
        assert latest.error_message == (
            "the process is stopping; the statement is retried after the "
            "uncounted delay"
        )
        # The original stays, recorded, for the retry.
        assert _count(pg_conn, "stored_files") == 1

        # The retry runs it as the same attempt, and completes it.
        state = attempt_state(get_item_history(pg_conn, raw.source_id))
        assert state.number == 1
        again = StopDuring(None, first_pass=_THREE_ACCOUNTS)
        outcome = _run(pg_conn, raw, store, again, state=state, control=Control(again))
        assert outcome.status == "success"
        assert again.calls == ["first_pass", "deposit", "deposit", "deposit"]
        assert _count(pg_conn, "statements") == 1
        assert _count(pg_conn, "stored_files") == 1
        assert len(_originals(tmp_path / "store")) == 1
        assert (
            _one(
                pg_conn,
                "SELECT attempt FROM receipt.ingest_log WHERE source_id = %(id)s"
                " AND status = 'success'",
                id=raw.source_id,
            )["attempt"]
            == 1
        )

    def test_a_stop_during_the_last_account_call_lets_it_finish(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        raw = _raw(make_pdf(_PAGE))
        replies = StopDuring(3, first_pass=_THREE_ACCOUNTS)
        outcome = _run(pg_conn, raw, store, replies, control=Control(replies))
        assert outcome.status == "success"
        assert _count(pg_conn, "statements") == 1

    def test_a_stop_before_the_first_pass_ends_it_with_no_model_call(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        raw = _raw(make_pdf(_PAGE))
        replies = StopDuring(None)
        replies.stopping = True
        outcome = _run(pg_conn, raw, store, replies, control=Control(replies))
        assert (outcome.status, outcome.error_class) == ("failed", "config")
        assert replies.calls == []


class TestOriginalRecordedWhenPut:
    def test_a_failed_run_leaves_a_recorded_original(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        raw = _raw(make_pdf(_PAGE))
        failing = Replies(account={**_ACCOUNT, "last4": "x"})
        assert _run(pg_conn, raw, store, failing).status == "failed"
        assert _count(pg_conn, "statements") == 0
        row = _one(
            pg_conn,
            "SELECT l.locator, l.purpose, l.owner FROM receipt.file_locations l"
            " JOIN receipt.stored_files f ON f.id = l.file_id",
        )
        assert (row["purpose"], row["owner"]) == ("original", "store")
        assert row["locator"].endswith("/statement.pdf")

    def test_a_retry_on_another_day_stores_no_second_copy(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        tmp_path: Path,
    ) -> None:
        data = make_pdf(_PAGE)
        raw = _raw(data)
        failing = Replies(account={**_ACCOUNT, "last4": "x"})
        assert _run(pg_conn, raw, store, failing).status == "failed"
        # The item comes back with another received date (a Drive item with
        # none is dated when it is fetched): its folder would differ.
        later = dataclasses.replace(raw, date=datetime(2027, 3, 9, tzinfo=UTC))
        state = attempt_state(get_item_history(pg_conn, raw.source_id))
        assert _run(pg_conn, later, store, Replies(), state=state).status == "success"
        assert len(_originals(tmp_path / "store")) == 1
        assert _count(pg_conn, "stored_files") == 1
        assert _count(pg_conn, "file_locations") == 1
        statement = _one(pg_conn, "SELECT original_file_id FROM receipt.statements")
        file_id = _one(pg_conn, "SELECT id FROM receipt.stored_files")["id"]
        assert statement["original_file_id"] == file_id

    def test_an_original_no_statement_points_at_is_no_error(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        raw = _raw(make_pdf(_PAGE))
        # Terminal: the item never gets a statement.
        failing = Replies(account={**_ACCOUNT, "last4": "x"})
        policy = RetryPolicy(max_attempts=1)
        assert _run(pg_conn, raw, store, failing, policy=policy).status == "failed"
        assert _count(pg_conn, "stored_files") == 1
        assert _count(pg_conn, "statements") == 0
        # Not an orphan to any report: those look at renditions only.
        assert orphaned_refs(pg_conn, None) == {}
        assert orphaned_refs(pg_conn, [raw.source_id]) == {}
        # Not counted as a statement of its source.
        assert count_statements_by_source(pg_conn) == {}
        # `failures` counts the item by its failed row, with no file.
        assert count_ingest_failures(pg_conn, status="terminal") == 1
        latest, *_ = get_item_history(pg_conn, raw.source_id)
        assert latest.stored_ref is None


class TestStatusCounts:
    def test_counts_by_source_and_current_status(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        data = make_pdf(_PAGE)
        assert _run(pg_conn, _raw(data), store, Replies()).status == "success"
        # The same bytes again: a copy, one statement, counted once.
        assert _run(pg_conn, _raw(data), store, Replies()).status == "success"
        rejected = Replies(first_pass={**_FIRST_PASS, "document_type": "other"})
        other = _raw(make_pdf("Synthetic letter page"))
        assert _run(pg_conn, other, store, rejected).status == "success"
        assert count_statements_by_source(pg_conn) == {
            _SOURCE: {"complete": 1, "incomplete": 0, "held": 0, "rejected": 1}
        }

    def test_failures_are_counted_per_statement_source_like_the_global_ones(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        now = datetime.now(UTC)
        rows = [
            # (item, type, source, next retry)
            ("due", STATEMENT_SOURCE_TYPE, _SOURCE, now - timedelta(minutes=1)),
            ("later", STATEMENT_SOURCE_TYPE, _SOURCE, now + timedelta(hours=1)),
            ("dead", STATEMENT_SOURCE_TYPE, _SOURCE, None),
            # A receipt item left under the name, and another source's item.
            ("receipt", "gdrive", _SOURCE, now + timedelta(hours=1)),
            ("elsewhere", STATEMENT_SOURCE_TYPE, "other", None),
        ]
        for item, kind, source, retry in rows:
            insert_ingest_log(
                pg_conn,
                source_id=f"{item}-{uuid.uuid4().hex}",
                source_type=kind,
                source_name=source,
                status="failed",
                error_class="content",
                next_retry_at=retry,
            )
        pg_conn.commit()
        assert count_statement_failures(pg_conn, _SOURCE) == {
            "pending": 1,
            "retrying": 1,
            "terminal": 1,
        }
        assert count_statement_failures(pg_conn, "none-yet") == {
            "pending": 0,
            "retrying": 0,
            "terminal": 0,
        }


class TestSelectDue:
    def test_a_source_selects_only_due_items_of_its_own_type(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        """A source switched between receipts and statements (review, U6).

        Its old items stay due but no pass of the other type selects them,
        so none is left "for later" on every round.
        """
        past = datetime.now(UTC) - timedelta(minutes=1)
        ids = {}
        for kind in (STATEMENT_SOURCE_TYPE, "gdrive"):
            ids[kind] = f"{kind}-{uuid.uuid4().hex}"
            insert_ingest_log(
                pg_conn,
                source_id=ids[kind],
                source_type=kind,
                source_name=_SOURCE,
                status="failed",
                error_class="content",
                next_retry_at=past,
            )
        pg_conn.commit()
        for kind in (STATEMENT_SOURCE_TYPE, "gdrive"):
            due = select_due(
                pg_conn, source_name=_SOURCE, source_type=kind, configured=[_SOURCE]
            )
            assert [d.source_id for d in due] == [ids[kind]]


class _Drive:
    """A statement folder: one listing, bodies by file ID."""

    folder = "statements-folder"

    def __init__(self, items: dict[str, bytes]) -> None:
        self.items = items

    def __enter__(self) -> _Drive:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def list_new(self, state: Any) -> SourceRead:
        return SourceRead(
            items=[
                ItemRef(
                    source_id=sid,
                    source_type="gdrive",
                    subject=_NAME,
                    file_name=_NAME,
                    drive_meta={"id": sid, "mimeType": "application/pdf"},
                )
                for sid in self.items
            ],
            drive_page_token="tok-1",
            drive_account_id="acct-1",
        )

    def fetch(self, ref: ItemRef) -> RawReceipt:
        data = self.items[ref.source_id]
        return RawReceipt(
            source_id=ref.source_id,
            source_name=_SOURCE,
            source_type="gdrive",
            date=datetime(2027, 2, 3, tzinfo=UTC),
            subject=ref.subject,
            file_name=ref.file_name,
            file_content=data,
            file_content_type="application/pdf",
            raw_bytes=data,
        )


class TestIntake:
    def test_a_statement_source_feeds_only_the_statement_pipeline(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def no_receipts(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a statement item reached process_item")

        monkeypatch.setattr(pipeline, "process_item", no_receipts)
        sid = f"drive-{uuid.uuid4().hex}"
        result = pipeline.run_ingest(
            conn=pg_conn,
            adapter=_Drive({sid: make_pdf(_PAGE)}),  # type: ignore[arg-type]
            store=store,
            source_name=_SOURCE,
            source_type="gdrive",
            item_type=STATEMENT_SOURCE_TYPE,
            llm_model="test-model",
            statement_agents=Replies().agents(),
        )
        assert (result.processed, result.failed, result.source_error) == (1, 0, None)
        assert _count(pg_conn, "statements") == 1
        rows = pg_conn.execute(
            "SELECT status, source_type, email_subject, email_sender"
            " FROM receipt.ingest_log WHERE source_id = %(id)s ORDER BY created_at",
            {"id": sid},
        ).fetchall()
        assert [r["status"] for r in rows] == ["started", "success"]
        for row in rows:
            assert row["source_type"] == STATEMENT_SOURCE_TYPE
            assert row["email_subject"] is None
            assert row["email_sender"] is None
        name = _one(
            pg_conn,
            "SELECT file_name FROM receipt.statement_sources WHERE source_id = %(id)s",
            id=sid,
        )["file_name"]
        assert name == "Statement_[number].pdf"
        # The sync state keeps the config type: its cursor columns are Drive's.
        (state,) = [s for s in list_sync_state(pg_conn) if s.source_name == _SOURCE]
        assert state.source_type == "gdrive"
        assert state.drive_page_token == "tok-1"
        # Nothing anywhere holds the number.
        for table in (
            "ingest_log",
            "statement_sources",
            "statements",
            "statement_revisions",
            "statement_accounts",
            "statement_lines",
            "statement_feed_state",
            "stored_files",
            "file_locations",
            "sync_state",
        ):
            dump = pg_conn.execute(
                f"SELECT string_agg(t::text, ' ') AS all FROM receipt.{table} t"
            ).fetchone()
            assert dump is not None
            assert _NUMBER not in (dump["all"] or ""), table

    def test_a_receipt_source_never_reaches_the_statement_pipeline(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from receipt_index import statements

        def no_statements(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a receipt item reached process_statement")

        seen: list[str] = []

        def receipt(_conn: Any, raw: RawReceipt, **kwargs: Any) -> Any:
            seen.append(raw.source_id)
            return pipeline.ItemOutcome("failed", error_class="content")

        monkeypatch.setattr(statements, "process_statement", no_statements)
        monkeypatch.setattr(pipeline, "process_item", receipt)
        sid = f"drive-{uuid.uuid4().hex}"
        result = pipeline.run_ingest(
            conn=pg_conn,
            adapter=_Drive({sid: make_pdf(_PAGE)}),  # type: ignore[arg-type]
            store=store,
            source_name=_SOURCE,
            source_type="gdrive",
            item_type="gdrive",
            llm_model="test-model",
            # Given, but a receipt source never uses them.
            statement_agents=Replies().agents(),
        )
        assert seen == [sid]
        assert result.source_error is None
        assert _count(pg_conn, "statements") == 0
        (row,) = pg_conn.execute(
            "SELECT source_type FROM receipt.ingest_log WHERE source_id = %(id)s",
            {"id": sid},
        ).fetchall()
        assert row["source_type"] == "gdrive"


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every record as a handler would write it, tracebacks included."""
    fmt = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(fmt.format(r) for r in caplog.records)


class _DownDrive(_Drive):
    """A statement folder whose errors quote a file name."""

    def __init__(self, items: dict[str, bytes], *, listing: bool) -> None:
        super().__init__(items)
        self.listing = listing

    def list_new(self, state: Any) -> SourceRead:
        if self.listing:
            msg = f"listing failed at {_NAME}"
            raise RuntimeError(msg)
        return super().list_new(state)

    def fetch(self, ref: ItemRef) -> RawReceipt:
        msg = f"server error fetching {_NAME}"
        raise SourceUnavailableError(msg)

    def fetch_by_id(self, source_id: str) -> RawReceipt | None:
        msg = f"server error fetching {_NAME}"
        raise SourceUnavailableError(msg)


class TestNoListingText:
    """A source's own error text never leaves a statement source (review, U6).

    Each error quotes a file name holding a synthetic card number; neither
    the sync state, the text `status` and pauses show, nor any log record
    may hold it.
    """

    def _ingest(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        *,
        listing: bool,
    ) -> pipeline.IngestResult:
        return pipeline.run_ingest(
            conn=pg_conn,
            adapter=_DownDrive(  # type: ignore[arg-type]
                {f"drive-{uuid.uuid4().hex}": make_pdf(_PAGE)}, listing=listing
            ),
            store=store,
            source_name=_SOURCE,
            source_type="gdrive",
            item_type=STATEMENT_SOURCE_TYPE,
            llm_model="test-model",
            statement_agents=Replies().agents(),
        )

    def _state_error(self, pg_conn: psycopg.Connection[dict[str, Any]]) -> str:
        (state,) = [s for s in list_sync_state(pg_conn) if s.source_name == _SOURCE]
        assert state.last_error is not None
        return state.last_error

    def test_a_listing_error_keeps_its_class_alone(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        result = self._ingest(pg_conn, store, listing=True)
        assert result.source_error == "read failed: RuntimeError"
        assert self._state_error(pg_conn) == "RuntimeError"
        assert "RuntimeError" in _logged(caplog)
        assert _NUMBER not in _logged(caplog)

    def test_a_source_down_while_fetching_keeps_its_class_alone(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        result = self._ingest(pg_conn, store, listing=False)
        assert result.source_error == "SourceUnavailableError"
        assert self._state_error(pg_conn) == "SourceUnavailableError"
        assert _NUMBER not in _logged(caplog)

    def test_a_source_down_in_the_retry_pass_keeps_its_class_alone(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        sid = f"drive-{uuid.uuid4().hex}"
        insert_ingest_log(
            pg_conn,
            source_id=sid,
            source_type=STATEMENT_SOURCE_TYPE,
            source_name=_SOURCE,
            status="failed",
            error_class="transient",
            counts_attempt=False,
            next_retry_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        pg_conn.commit()
        due = select_due(
            pg_conn,
            source_name=_SOURCE,
            source_type=STATEMENT_SOURCE_TYPE,
            configured=[_SOURCE],
        )
        config = GdriveSourceConfig(
            name=_SOURCE,
            type="gdrive",
            folder_id="statements-folder",
            token_json="{}",
            documents="statements",
        )
        with ExitStack() as stack:
            down: Any = _DownDrive({}, listing=False)
            pool = SourcePool([config], lambda _c: down, stack)
            result = pipeline.run_due_retries(
                pg_conn,
                due,
                pool=pool,
                store=store,
                agents={},
                llm_model="test-model",
                own_source=_SOURCE,
                statement_agents=Replies().agents(),
            )
        assert result.stopped == f"source {_SOURCE} unavailable: SourceUnavailableError"
        assert _NUMBER not in _logged(caplog)
        # Left as it was: no started row, no attempt used.
        latest, *_ = get_item_history(pg_conn, sid)
        assert latest.status == "failed"
