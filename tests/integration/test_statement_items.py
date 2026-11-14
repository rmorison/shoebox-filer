"""A statement item's name and its done-ness, against Postgres (plan U5).

A Drive file name can hold an account number, joined in ways a description
never is, or be no valid store name at all: neither may reach a stored row
or path, and neither may fail the item. Synthetic names and PDFs only.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from receipt_index import pipeline
from receipt_index.fetching import SourcePool
from receipt_index.repository import get_item_history, insert_ingest_log, select_due
from receipt_index.statements import STATEMENT_SOURCE_TYPE
from tests.integration.test_statement_pipeline import (
    _PAGE,
    _SOURCE,
    Replies,
    _one,
    _raw,
    _run,
    store,
)
from tests.pdfs import make_pdf

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import psycopg

    from receipt_index.models import RawReceipt
    from receipt_index.store import LocalEvidenceStore

pytestmark = pytest.mark.integration

__all__ = ["store"]  # the fixture, shared with the pipeline tests

# A synthetic card number: no group of it may reach a stored row or path.
_GROUPS = ("4929", "3817", "6620", "7319")
_HIDDEN = _GROUPS


def _sinks(conn: psycopg.Connection[dict[str, Any]], store_root: Path) -> list[str]:
    """Every stored place a source's file name could reach."""
    texts: list[str] = []
    for sql in (
        "SELECT file_name AS t FROM receipt.statement_sources",
        "SELECT locator AS t FROM receipt.file_locations",
        "SELECT display_path AS t FROM receipt.file_locations",
        "SELECT email_subject AS t FROM receipt.ingest_log",
        "SELECT error_message AS t FROM receipt.ingest_log",
    ):
        texts += [row["t"] for row in conn.execute(sql).fetchall() if row["t"]]
    texts += [str(p.relative_to(store_root)) for p in store_root.rglob("*")]
    return texts


class TestFileNames:
    @pytest.mark.parametrize(
        "name",
        [
            "Statement_{}_{}_{}_{}.pdf",
            "{}#{}#{}#{}.pdf",
            "Stmt({})({})({})({}).pdf",
            "{}.{}.{}.{}.pdf",
            "Statement {} {} {} {}.pdf",
            "{}%2D{}%2D{}%2D{}.pdf",
            "{}%5F{}%5F{}%5F{}.pdf",
            "{}%2E{}%2E{}%2E{}.pdf",
            "{}x{}x{}x{}.pdf",
            "{}-visa-{}-{}-{}.pdf",
            "Statement%5F{}%5F{}%5F{}%5F{}.pdf",
            "{}%252D{}%252D{}%252D{}.pdf",
            "{}a{}b{}c{}.pdf",
            "{}\u02b0{}\u02b0{}\u02b0{}.pdf",
            "acct {} {} then {} {}.pdf",
        ],
    )
    def test_a_card_number_in_a_file_name_reaches_no_stored_row_or_path(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        tmp_path: Path,
        name: str,
    ) -> None:
        raw = _raw(make_pdf(_PAGE), name=name.format(*_GROUPS))
        outcome = _run(pg_conn, raw, store, Replies())
        assert outcome.status == "success"
        texts = _sinks(pg_conn, tmp_path / "store")
        assert texts, "nothing was stored, so nothing was checked"
        assert not [t for t in texts for group in _HIDDEN if group in t], texts
        stored = _one(pg_conn, "SELECT file_name FROM receipt.statement_sources")
        assert "[number]" in stored["file_name"]  # cut, with no digits kept
        # The store names every original the same: the folder carries the
        # date, the source and the hash.
        located = _one(pg_conn, "SELECT locator FROM receipt.file_locations")
        assert located["locator"].endswith("/statement.pdf")

    @pytest.mark.parametrize(
        "name",
        [
            "Statement 01/2027.pdf",
            "Statement 01\\2027.pdf",
            ".statement.pdf",
            "..",
            "S" * 300 + ".pdf",
            "bad\ud800.pdf",
            "",
        ],
    )
    def test_no_file_name_fails_an_item(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        name: str,
    ) -> None:
        raw = _raw(make_pdf(_PAGE), name=name)
        outcome = _run(pg_conn, raw, store, Replies())
        assert (outcome.status, outcome.error_class) == ("success", None)
        (latest, *_) = get_item_history(pg_conn, raw.source_id)
        assert latest.status == "success"
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.statements")["n"] == 1


class TestRaceAtTheLastAttempt:
    def test_losing_the_same_bytes_race_on_the_last_attempt_costs_nothing(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two sources hold the same PDF, and the second source's item is on
        # its last attempt. The first writes the statement while the
        # second's models run: the second ends as a copy, not as a terminal
        # failure.
        from receipt_index import statements
        from receipt_index.retries import AttemptState, RetryPolicy

        data = make_pdf(_PAGE)
        first = _raw(data)
        second = _raw(data, source_id="drive-last-attempt")
        policy = RetryPolicy()
        real_extract = statements.extract_statement
        pending = [first]

        def extract(*args: Any, **kwargs: Any) -> Any:
            extracted = real_extract(*args, **kwargs)
            if pending:
                assert _run(pg_conn, pending.pop(), store, Replies()).status == (
                    "success"
                )
            return extracted

        monkeypatch.setattr(statements, "extract_statement", extract)
        outcome = _run(
            pg_conn,
            second,
            store,
            Replies(),
            policy=policy,
            state=AttemptState(policy.max_attempts),
        )
        assert (outcome.status, outcome.error_class) == ("success", None)
        (latest, *_) = get_item_history(pg_conn, second.source_id)
        assert latest.status == "success"
        assert not _one(
            pg_conn,
            "SELECT count(*) AS n FROM receipt.ingest_log WHERE status = 'failed'",
        )["n"]
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.statements")["n"] == 1


class TestDone:
    def test_an_item_that_finished_after_selection_is_left_alone(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Selected as due; before the retry pass takes its lock, another
        # worker delivers its statement, and a requeue lands after it. Its
        # latest row is due again, but it is done: the pass must leave it,
        # with no fetch and no row.
        raw = _raw(make_pdf(_PAGE), source_id="drive-finished-late")
        insert_ingest_log(
            pg_conn,
            source_id=raw.source_id,
            source_type=STATEMENT_SOURCE_TYPE,
            source_name=_SOURCE,
            status="failed",
            error_message="earlier failure",
            error_class="content",
            next_retry_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        due = select_due(
            pg_conn,
            source_name=_SOURCE,
            source_type=STATEMENT_SOURCE_TYPE,
            configured=[_SOURCE],
        )
        assert [entry.source_id for entry in due] == [raw.source_id]

        real_lock = pipeline.item_lock

        @contextmanager
        def lock_after_another_worker(conn: Any, source_id: str) -> Iterator[bool]:
            assert _run(conn, raw, store, Replies()).status == "success"
            insert_ingest_log(
                conn,
                source_id=source_id,
                source_type=STATEMENT_SOURCE_TYPE,
                source_name=_SOURCE,
                status="queued",
            )
            conn.commit()
            with real_lock(conn, source_id) as locked:
                yield locked

        monkeypatch.setattr(pipeline, "item_lock", lock_after_another_worker)
        fetched: list[str] = []

        class Adapter:
            def __enter__(self) -> Adapter:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def fetch_by_id(self, source_id: str) -> RawReceipt:
                fetched.append(source_id)
                return raw

        rows_before = _one(pg_conn, "SELECT count(*) AS n FROM receipt.ingest_log")
        config = SimpleNamespace(name=_SOURCE, type="gdrive", documents="statements")
        with ExitStack() as stack:
            pool = SourcePool([config], lambda _: Adapter(), stack)  # type: ignore[list-item, arg-type, return-value]
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
        assert fetched == []
        assert (result.processed, result.failed) == (0, 0)
        rows_after = _one(pg_conn, "SELECT count(*) AS n FROM receipt.ingest_log")
        # The other worker's started, success and queued rows, and no more.
        assert rows_after["n"] == rows_before["n"] + 3


class TestFeedLock:
    def test_an_item_waits_on_the_feed_lock_and_ends_as_a_copy(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The same bytes' original is already recorded (an earlier attempt
        # stored it), so recording it again waits on nothing. Another
        # connection holds the feed lock with the same bytes' statement
        # written but not committed. The item reaches its write transaction
        # and must wait on the lock; once the holder commits, it ends as a
        # copy, with no attempt counted. Without the lock it would insert a
        # second statement for the same original and fail on the unique
        # constraint once the holder commits.
        import hashlib
        import threading

        import psycopg as pg
        from psycopg.rows import dict_row

        from receipt_index import statements
        from receipt_index.originals import put_original
        from receipt_index.repository import record_original
        from receipt_index.store import StoreFolder
        from tests.integration.conftest import _DB_DSN

        data = make_pdf(_PAGE)
        digest = hashlib.sha256(data).digest()
        holder_raw = _raw(data, source_id="drive-lock-holder")
        item = _raw(data, source_id="drive-lock-waiter")
        assert holder_raw.date is not None
        stored = put_original(
            pg_conn,
            store,
            data,
            media_type="application/pdf",
            folder=StoreFolder.for_original(holder_raw.date, _SOURCE, digest),
            name="statement.pdf",
        )
        record_original(
            pg_conn,
            ref=stored.ref,
            sha256=digest,
            byte_size=len(data),
            media_type="application/pdf",
        )
        pg_conn.commit()

        holder = pg.connect(_DB_DSN, row_factory=dict_row)
        watcher = pg.connect(_DB_DSN, row_factory=dict_row, autocommit=True)
        written, go, waited = threading.Event(), threading.Event(), threading.Event()
        real_add_source = statements.add_source

        def add_source(conn: Any, **kwargs: Any) -> None:
            real_add_source(conn, **kwargs)
            if conn is holder:
                written.set()  # the statement is in, the feed lock held
                assert go.wait(30), "the item never waited"

        monkeypatch.setattr(statements, "add_source", add_source)
        outcomes: dict[str, Any] = {}

        def hold() -> None:
            outcomes["holder"] = _run(holder, holder_raw, store, Replies())

        def release_when_the_item_waits() -> None:
            pid = pg_conn.info.backend_pid
            for _ in range(600):
                row = watcher.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    (pid,),
                ).fetchone()
                if row is not None and row["wait_event_type"] == "Lock":
                    waited.set()
                    break
                threading.Event().wait(0.05)
            go.set()  # the holder commits

        threads = [threading.Thread(target=hold)]
        try:
            threads[0].start()
            assert written.wait(30), "the holder never wrote"
            threads.append(threading.Thread(target=release_when_the_item_waits))
            threads[1].start()
            outcomes["item"] = _run(pg_conn, item, store, Replies())
        finally:
            go.set()
            for thread in threads:
                thread.join(30)
            holder.close()
            watcher.close()

        assert waited.is_set(), "the item never waited on a lock"
        assert outcomes["holder"].status == "success"
        assert (outcomes["item"].status, outcomes["item"].error_class) == (
            "success",
            None,
        )
        (latest, *older) = get_item_history(pg_conn, item.source_id)
        assert latest.status == "success"
        assert latest.error_class is None
        assert [row.status for row in older] == ["started"]
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.statements")["n"] == 1
        assert (
            _one(pg_conn, "SELECT count(*) AS n FROM receipt.statement_sources")["n"]
            == 2
        )
