"""The statement pipeline against Postgres, with stubbed models (plan U5).

Every statement here is a synthetic PDF built by the test, and every model
reply is the test's own: no real statement, institution or amount.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from receipt_index import pipeline
from receipt_index.fetching import SourcePool
from receipt_index.models import RawReceipt
from receipt_index.repository import (
    RetryFilter,
    get_item_history,
    insert_ingest_log,
    select_due,
    select_retry_candidates,
)
from receipt_index.requeue import apply_requeue
from receipt_index.retries import AttemptState, RetryPolicy
from receipt_index.statement_extraction import (
    create_account_agent,
    create_first_pass_agent,
)
from receipt_index.statements import (
    STATEMENT_SOURCE_TYPE,
    StatementAgents,
    process_statement,
)
from receipt_index.store import LocalEvidenceStore
from tests.pdfs import make_pdf

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import psycopg

pytestmark = pytest.mark.integration

_SOURCE = "statements-drive"
_PAGE = "Synthetic Savings Bank statement page"

_FIRST_PASS: dict[str, Any] = {
    "document_type": "deposit",
    "accounts": [
        {
            "label": "Checking ending 1234",
            "account_type": "deposit",
            "first_page": 1,
            "last_page": 1,
        }
    ],
    "printed_account_count": 1,
    "period": {
        "start": {"month": 1, "day": 1, "year": 2027},
        "end": {"month": 1, "day": 31, "year": 2027},
    },
}
_ACCOUNT: dict[str, Any] = {
    "institution": "Synthetic Savings Bank",
    "last4": "1234",
    "currency": "USD",
    "beginning_balance": "100.00",
    "ending_balance": "115.00",
    "line_order": "oldest_first",
    "lines": [
        {
            "posted": {"month": 1, "day": 5, "year": None},
            "description": "Synthetic grocery purchase",
            "amount": "-10.00",
            "running_balance": "90.00",
            "page": 1,
        },
        {
            "posted": {"month": 1, "day": 9, "year": None},
            "description": "Synthetic payroll deposit",
            "amount": "25.00",
            "running_balance": "115.00",
            "page": 1,
        },
    ],
}


class Replies:
    """Stubbed models: one reply for the first pass, one for each account."""

    def __init__(
        self,
        first_pass: dict[str, Any] | None = None,
        account: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.first_pass = first_pass or _FIRST_PASS
        self.account = account or _ACCOUNT
        self.calls: list[str] = []

    def _model(self, kind: str) -> FunctionModel:
        def reply(_: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.calls.append(kind)
            if kind == "first_pass":
                args = self.first_pass
            else:
                args = self.account() if callable(self.account) else self.account
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

        return FunctionModel(reply)

    def agents(self) -> StatementAgents:
        return StatementAgents(
            first_pass=create_first_pass_agent(self._model("first_pass")),
            accounts={
                kind: create_account_agent(kind, self._model(kind))
                for kind in ("deposit", "card", "loan")
            },
        )


class Steps:
    """A RunControl that records each watchdog step."""

    def __init__(self) -> None:
        self.steps: list[str] = []

    def should_stop(self) -> bool:
        return False

    def running(self, source_id: str) -> Any:
        from contextlib import nullcontext

        return nullcontext()

    def step(self, source_id: str) -> None:
        self.steps.append(source_id)


def _raw(
    data: bytes, *, source_id: str | None = None, name: str = "statement.pdf"
) -> RawReceipt:
    return RawReceipt(
        source_id=source_id or f"drive-{uuid.uuid4().hex}",
        source_name=_SOURCE,
        source_type="gdrive",
        date=datetime(2027, 2, 3, tzinfo=UTC),
        subject=name,
        sender="",
        file_name=name,
        file_content=data,
        file_content_type="application/pdf",
        raw_bytes=data,
    )


def _started(conn: psycopg.Connection[dict[str, Any]], raw: RawReceipt) -> None:
    insert_ingest_log(
        conn,
        source_id=raw.source_id,
        source_type=STATEMENT_SOURCE_TYPE,
        source_name=_SOURCE,
        status="started",
    )


def _run(
    conn: psycopg.Connection[dict[str, Any]],
    raw: RawReceipt,
    store: LocalEvidenceStore,
    replies: Replies,
    **kwargs: Any,
) -> Any:
    _started(conn, raw)
    return process_statement(
        conn,
        raw,
        store=store,
        source_name=_SOURCE,
        agents=replies.agents(),
        llm_model="test-model",
        **kwargs,
    )


def _one(conn: psycopg.Connection[dict[str, Any]], sql: str, **params: Any) -> Any:
    return conn.execute(sql, params).fetchone()


@pytest.fixture
def store(tmp_path: Path) -> LocalEvidenceStore:
    root = tmp_path / "store"
    root.mkdir()
    return LocalEvidenceStore(root)


def _originals(store_root: Path) -> list[Path]:
    return [p for p in (store_root / "originals").rglob("*") if p.is_file()]


class TestEndToEnd:
    def test_a_clean_deposit_statement_is_complete(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        tmp_path: Path,
    ) -> None:
        replies = Replies()
        raw = _raw(make_pdf(_PAGE), name="Statement 4111 1111 1111 1234.pdf")
        steps = Steps()
        outcome = _run(pg_conn, raw, store, replies, control=steps)
        assert outcome.status == "success"

        statement = _one(
            pg_conn,
            "SELECT s.status, s.document_type, r.masked_runs, r.pages,"
            " r.input_tokens, r.llm_model FROM receipt.statements s"
            " JOIN receipt.statement_revisions r ON r.id = s.current_revision_id",
        )
        assert (statement["status"], statement["document_type"]) == (
            "complete",
            "deposit",
        )
        assert statement["pages"] == 1
        assert statement["llm_model"] == "test-model"
        lines = pg_conn.execute(
            "SELECT description, amount FROM receipt.statement_lines ORDER BY ordinal"
        ).fetchall()
        assert [line["description"] for line in lines] == [
            "Synthetic grocery purchase",
            "Synthetic payroll deposit",
        ]
        source = _one(pg_conn, "SELECT file_name FROM receipt.statement_sources")
        # The file name is masked before it is stored.
        assert source["file_name"] == "Statement 1234.pdf"
        (latest, *_) = get_item_history(pg_conn, raw.source_id)
        assert (latest.status, latest.source_type) == ("success", STATEMENT_SOURCE_TYPE)
        assert replies.calls == ["first_pass", "deposit"]
        # A step for the read, the first pass and the one account call.
        assert steps.steps == [raw.source_id] * 3
        assert len(_originals(tmp_path / "store")) == 1

    def test_the_same_bytes_from_two_drops_are_one_statement(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        data = make_pdf(_PAGE)
        replies = Replies()
        _run(pg_conn, _raw(data), store, replies)
        again = Replies()
        outcome = _run(pg_conn, _raw(data), store, again)
        assert outcome.status == "success"
        assert again.calls == []  # no model call for a copy
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.statements")["n"] == 1
        assert (
            _one(pg_conn, "SELECT count(*) AS n FROM receipt.statement_sources")["n"]
            == 2
        )

    def test_the_same_bytes_written_during_the_model_calls_make_a_copy(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two sources hold the same PDF. The second source's item passes the
        # copy check, then, while its models run, the first source's item
        # writes the statement. The second must end as a copy: a source row,
        # a success, no new revision, no error and no attempt.
        from receipt_index import statements

        data = make_pdf(_PAGE)
        first, second = _raw(data), _raw(data)
        first_replies, second_replies = Replies(), Replies()
        real_extract = statements.extract_statement
        raced: list[Any] = []
        pending = [first]

        def extract(*args: Any, **kwargs: Any) -> Any:
            extracted = real_extract(*args, **kwargs)
            if pending:  # once, for the second item: the first one wins
                raced.append(_run(pg_conn, pending.pop(), store, first_replies))
            return extracted

        monkeypatch.setattr(statements, "extract_statement", extract)
        outcome = _run(pg_conn, second, store, second_replies)

        assert [o.status for o in raced] == ["success"]
        assert (outcome.status, outcome.error_class) == ("success", None)
        # Both items ran their models; only one statement and one revision.
        assert first_replies.calls == second_replies.calls == ["first_pass", "deposit"]
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.statements")["n"] == 1
        assert (
            _one(pg_conn, "SELECT count(*) AS n FROM receipt.statement_revisions")["n"]
            == 1
        )
        sources = pg_conn.execute(
            "SELECT source_id FROM receipt.statement_sources ORDER BY source_id"
        ).fetchall()
        assert sorted(s["source_id"] for s in sources) == sorted(
            [first.source_id, second.source_id]
        )
        (latest, *older) = get_item_history(pg_conn, second.source_id)
        assert latest.status == "success"
        assert [row.status for row in older] == ["started"]
        # The wasted run's tokens are still on its success row.
        row = _one(
            pg_conn,
            "SELECT llm_requests, llm_model FROM receipt.ingest_log"
            " WHERE source_id = %(id)s AND status = 'success'",
            id=second.source_id,
        )
        assert row["llm_requests"] == 2
        assert row["llm_model"] == "test-model"
        # One stored file, every location of it recorded: no orphan.
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.stored_files")["n"] == 1
        locations = _one(pg_conn, "SELECT count(*) AS n FROM receipt.file_locations")[
            "n"
        ]
        assert locations == len(_originals(tmp_path / "store"))

    def test_a_scan_is_rejected_with_no_model_call(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        replies = Replies()
        outcome = _run(pg_conn, _raw(make_pdf(None, pages=2)), store, replies)
        assert outcome.status == "success"
        row = _one(
            pg_conn,
            "SELECT s.status, s.document_type, r.llm_model FROM receipt.statements s"
            " JOIN receipt.statement_revisions r ON r.id = s.current_revision_id",
        )
        assert (row["status"], row["document_type"], row["llm_model"]) == (
            "rejected",
            "scan",
            None,
        )
        assert replies.calls == []

    def test_a_document_that_isnt_a_statement_is_rejected_by_type(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        replies = Replies(first_pass={"document_type": "loan_bill"})
        _run(pg_conn, _raw(make_pdf(_PAGE)), store, replies)
        row = _one(pg_conn, "SELECT status, document_type FROM receipt.statements")
        assert (row["status"], row["document_type"]) == ("rejected", "loan_bill")
        assert replies.calls == ["first_pass"]

    def test_more_accounts_than_the_cap_is_rejected_over_bounds(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        listed = _FIRST_PASS["accounts"][0]
        replies = Replies(first_pass={**_FIRST_PASS, "accounts": [listed] * 9})
        _run(pg_conn, _raw(make_pdf(_PAGE)), store, replies)
        row = _one(
            pg_conn,
            "SELECT document_type, rejected_bound FROM receipt.statement_revisions",
        )
        assert (row["document_type"], row["rejected_bound"]) == (
            "over_bounds",
            "accounts",
        )

    def test_an_account_past_the_last_page_is_dropped_without_a_call(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        past = {**_FIRST_PASS["accounts"][0], "first_page": 1, "last_page": 3}
        replies = Replies(first_pass={**_FIRST_PASS, "accounts": [past]})
        _run(pg_conn, _raw(make_pdf(_PAGE)), store, replies)
        row = _one(pg_conn, "SELECT status FROM receipt.statements")
        revision = _one(pg_conn, "SELECT hold_reason FROM receipt.statement_revisions")
        assert (row["status"], revision["hold_reason"]) == ("held", "dropped_account")
        assert replies.calls == ["first_pass"]


class TestFailures:
    def test_a_schema_error_writes_no_revision_and_is_retried(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        number = "5274910386254173"
        replies = Replies(account={**_ACCOUNT, "last4": f"not {number}"})
        raw = _raw(make_pdf(_PAGE))
        outcome = _run(pg_conn, raw, store, replies, policy=RetryPolicy())
        assert (outcome.status, outcome.error_class) == ("failed", "content")
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.statements")["n"] == 0
        (latest, *_) = get_item_history(pg_conn, raw.source_id)
        assert latest.status == "failed"
        assert latest.counts_attempt
        assert latest.error_message is not None
        assert number not in latest.error_message
        assert "schema" in latest.error_message

    def test_a_crash_after_the_original_is_stored_resumes_without_a_second_copy(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        tmp_path: Path,
    ) -> None:
        data = make_pdf(_PAGE)
        failing = Replies(account={**_ACCOUNT, "last4": "x"})
        raw = _raw(data)
        assert _run(pg_conn, raw, store, failing).status == "failed"
        assert len(_originals(tmp_path / "store")) == 1
        outcome = _run(pg_conn, raw, store, Replies(), state=AttemptState(2))
        assert outcome.status == "success"
        assert len(_originals(tmp_path / "store")) == 1
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.stored_files")["n"] == 1


class TestRetries:
    def _failed_statement(
        self, conn: psycopg.Connection[dict[str, Any]], raw: RawReceipt
    ) -> None:
        insert_ingest_log(
            conn,
            source_id=raw.source_id,
            source_type=STATEMENT_SOURCE_TYPE,
            source_name=_SOURCE,
            status="failed",
            error_message="earlier failure",
            error_class="content",
            next_retry_at=datetime(2000, 1, 1, tzinfo=UTC),
        )

    def test_a_due_statement_item_goes_to_the_statement_pipeline(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        raw = _raw(make_pdf(_PAGE))
        self._failed_statement(pg_conn, raw)

        def no_receipts(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a statement item reached process_item")

        monkeypatch.setattr(pipeline, "process_item", no_receipts)
        due = select_due(
            pg_conn,
            source_name=_SOURCE,
            source_type=STATEMENT_SOURCE_TYPE,
            configured=[_SOURCE],
        )
        assert [entry.source_id for entry in due] == [raw.source_id]
        # A receipt source of type gdrive is never offered the statement row.
        assert not select_due(
            pg_conn,
            source_name="receipts-drive",
            source_type="gdrive",
            configured=[_SOURCE, "receipts-drive"],
        )

        class Adapter:
            def __enter__(self) -> Adapter:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def fetch_by_id(self, source_id: str) -> RawReceipt:
                return raw

        adapter = Adapter()
        config = SimpleNamespace(name=_SOURCE, type="gdrive")
        with ExitStack() as stack:
            pool = SourcePool([config], lambda _: adapter, stack)  # type: ignore[list-item, arg-type, return-value]
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
        assert result.processed == 1
        # Done now: not due, and not offered by `retry`.
        assert not select_due(
            pg_conn,
            source_name=_SOURCE,
            source_type=STATEMENT_SOURCE_TYPE,
            configured=[_SOURCE],
        )
        assert not select_retry_candidates(pg_conn, RetryFilter(status="failed"))

    def test_an_item_whose_source_was_removed_is_fetched_by_a_statement_source(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Its source is gone from the config: the retry falls back to the
        # configured sources of its ingest type, so a statement Drive source
        # re-fetches it and a receipt Drive source (same config type) never
        # sees it.
        raw = _raw(make_pdf(_PAGE))
        insert_ingest_log(
            pg_conn,
            source_id=raw.source_id,
            source_type=STATEMENT_SOURCE_TYPE,
            source_name="removed-statements",
            status="failed",
            error_message="earlier failure",
            error_class="content",
            next_retry_at=datetime(2000, 1, 1, tzinfo=UTC),
        )

        def no_receipts(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a statement item reached process_item")

        monkeypatch.setattr(pipeline, "process_item", no_receipts)
        fetched_by: list[str] = []

        class Adapter:
            def __init__(self, name: str) -> None:
                self.name = name

            def __enter__(self) -> Adapter:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def fetch_by_id(self, source_id: str) -> RawReceipt:
                fetched_by.append(self.name)
                if self.name != "statements-drive":
                    raise AssertionError("a receipt source fetched a statement")
                return raw

        configs = [
            SimpleNamespace(name="receipts-drive", type="gdrive"),
            SimpleNamespace(
                name="statements-drive", type="gdrive", documents="statements"
            ),
        ]
        configured = [c.name for c in configs]
        # Offered to the statement source, never to the receipt one.
        assert not select_due(
            pg_conn,
            source_name="receipts-drive",
            source_type="gdrive",
            configured=configured,
        )
        due = select_due(
            pg_conn,
            source_name="statements-drive",
            source_type=STATEMENT_SOURCE_TYPE,
            configured=configured,
        )
        assert [entry.source_id for entry in due] == [raw.source_id]
        with ExitStack() as stack:
            pool = SourcePool(configs, lambda c: Adapter(c.name), stack)  # type: ignore[arg-type, return-value]
            result = pipeline.run_due_retries(
                pg_conn,
                due,
                pool=pool,
                store=store,
                agents={},
                llm_model="test-model",
                own_source="statements-drive",
                statement_agents=Replies().agents(),
            )
        assert result.processed == 1
        assert fetched_by == ["statements-drive"]
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.statements")["n"] == 1

    def test_a_drive_error_on_a_statement_retry_is_kept_by_its_class(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A source's error can quote the item: a Drive file name can hold an
        # account number. On a statement item it reaches no log record, no
        # traceback and no ingest_log row; only its class does.
        digits = "4929381766207319"
        raw = _raw(make_pdf(_PAGE))
        self._failed_statement(pg_conn, raw)

        class Adapter:
            def __enter__(self) -> Adapter:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def fetch_by_id(self, source_id: str) -> RawReceipt:
                msg = f"can't download 'Statement {digits}.pdf'"
                raise RuntimeError(msg)

        due = select_due(
            pg_conn,
            source_name=_SOURCE,
            source_type=STATEMENT_SOURCE_TYPE,
            configured=[_SOURCE],
        )
        config = SimpleNamespace(name=_SOURCE, type="gdrive", documents="statements")
        caplog.set_level(logging.DEBUG)
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
        assert result.failed == 1
        formatter = logging.Formatter()
        logged = [
            record.getMessage()
            + (formatter.formatException(record.exc_info) if record.exc_info else "")
            for record in caplog.records
        ]
        assert any("RuntimeError" in text for text in logged), logged
        assert not [text for text in logged if digits[-4:] in text]
        (latest, *_) = get_item_history(pg_conn, raw.source_id)
        assert (latest.status, latest.error_message) == ("failed", "RuntimeError")
        messages = pg_conn.execute(
            "SELECT error_message FROM receipt.ingest_log"
            " WHERE error_message IS NOT NULL"
        ).fetchall()
        assert not [m for m in messages if digits[-4:] in m["error_message"]]

    def test_an_item_that_delivered_a_statement_is_never_due_again(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        # Its statement_sources row makes it done, whatever its latest row
        # says: a stale failure after the success isn't due or retried.
        raw = _raw(make_pdf(_PAGE))
        assert _run(pg_conn, raw, store, Replies()).status == "success"
        self._failed_statement(pg_conn, raw)
        assert not select_due(
            pg_conn,
            source_name=_SOURCE,
            source_type=STATEMENT_SOURCE_TYPE,
            configured=[_SOURCE],
        )
        assert not select_retry_candidates(pg_conn, RetryFilter(status="failed"))

    def test_a_requeued_statement_item_stays_a_statement_item(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
    ) -> None:
        raw = _raw(make_pdf(_PAGE))
        self._failed_statement(pg_conn, raw)
        spec = RetryFilter(status="failed")
        candidates = select_retry_candidates(pg_conn, spec)
        assert [c.source_id for c in candidates] == [raw.source_id]
        apply_requeue(pg_conn, spec, candidates)
        (latest, *_) = get_item_history(pg_conn, raw.source_id)
        assert (latest.status, latest.source_type) == ("queued", STATEMENT_SOURCE_TYPE)
        due = select_due(
            pg_conn,
            source_name=_SOURCE,
            source_type=STATEMENT_SOURCE_TYPE,
            configured=[_SOURCE],
        )
        assert [entry.source_id for entry in due] == [raw.source_id]


def test_the_synthetic_pdf_reads_as_text() -> None:
    # A guard on the fixture: the end-to-end tests need a text PDF.
    from receipt_index.pdf_reader import read_statement

    read = read_statement(make_pdf(_PAGE))
    assert read.reason is None
    assert read.page_texts is not None
    assert (
        hashlib.sha256(make_pdf(_PAGE)).digest()
        == hashlib.sha256(make_pdf(_PAGE)).digest()
    )


class TestRetriesWithoutAgents:
    def test_a_pass_without_statement_agents_leaves_the_item_untouched(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        # No fetch, no started row: a started row would be closed next pass
        # as a counted failure, using up the item's attempts.
        raw = _raw(make_pdf(_PAGE))
        TestRetries()._failed_statement(pg_conn, raw)
        before = get_item_history(pg_conn, raw.source_id)

        class NoFetch:
            def __enter__(self) -> NoFetch:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def fetch_by_id(self, source_id: str) -> RawReceipt:
                raise AssertionError("fetched without statement agents")

        due = select_due(
            pg_conn,
            source_name=_SOURCE,
            source_type=STATEMENT_SOURCE_TYPE,
            configured=[_SOURCE],
        )
        config = SimpleNamespace(name=_SOURCE, type="gdrive")
        for _ in range(2):
            with ExitStack() as stack:
                pool = SourcePool([config], lambda _: NoFetch(), stack)  # type: ignore[list-item, arg-type, return-value]
                result = pipeline.run_due_retries(
                    pg_conn,
                    due,
                    pool=pool,
                    store=store,
                    agents={},
                    llm_model="test-model",
                    own_source=_SOURCE,
                )
            assert (result.processed, result.failed) == (0, 0)
        assert get_item_history(pg_conn, raw.source_id) == before


def _read(reason: str | None, pages: int = 1) -> Any:
    from receipt_index.pdf_reader import StatementRead

    return StatementRead(None, pages=pages, reason=reason, encrypted=False)  # type: ignore[arg-type]


class TestErrorPaths:
    @pytest.mark.parametrize("reason", ["timeout", "failed"])
    def test_a_read_that_stopped_is_a_retried_item_error(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
        reason: str,
    ) -> None:
        from receipt_index import statements

        monkeypatch.setattr(statements, "read_statement", lambda *a, **k: _read(reason))
        replies = Replies()
        raw = _raw(make_pdf(_PAGE))
        outcome = _run(pg_conn, raw, store, replies)
        assert (outcome.status, outcome.error_class) == ("failed", "content")
        assert not outcome.stops_source
        assert replies.calls == []
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.statements")["n"] == 0
        (latest, *_) = get_item_history(pg_conn, raw.source_id)
        assert (latest.status, latest.counts_attempt) == ("failed", True)

    def test_a_reader_that_couldnt_start_is_a_retried_item_error(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A parent-side OSError (no processes, or a full temporary directory)
        # propagates out of read_statement; U5 retries it like a stopped read.
        import errno

        from receipt_index import statements

        def no_reader(*_a: object, **_k: object) -> None:
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

        monkeypatch.setattr(statements, "read_statement", no_reader)
        replies = Replies()
        raw = _raw(make_pdf(_PAGE))
        outcome = _run(pg_conn, raw, store, replies)
        assert (outcome.status, outcome.error_class) == ("failed", "content")
        assert replies.calls == []
        (latest, *_) = get_item_history(pg_conn, raw.source_id)
        assert (latest.status, latest.counts_attempt) == ("failed", True)
        assert latest.next_retry_at is not None
        assert latest.error_message == (
            "the statement reader couldn't start: "
            "BlockingIOError: Resource temporarily unavailable"
        )

    @pytest.mark.parametrize("reason", ["timeout", "failed"])
    def test_a_stopped_read_is_terminal_at_the_last_attempt(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
        reason: str,
    ) -> None:
        # Bounded: a file that really does exhaust the host is retried a
        # fixed number of times, never forever.
        from receipt_index import statements
        from receipt_index.retries import AttemptState, RetryPolicy

        monkeypatch.setattr(statements, "read_statement", lambda *a, **k: _read(reason))
        policy = RetryPolicy()
        raw = _raw(make_pdf(_PAGE))
        outcome = _run(
            pg_conn,
            raw,
            store,
            Replies(),
            policy=policy,
            state=AttemptState(policy.max_attempts),
        )
        assert (outcome.status, outcome.error_class) == ("failed", "content")
        (latest, *_) = get_item_history(pg_conn, raw.source_id)
        assert latest.attempt == policy.max_attempts
        assert latest.counts_attempt
        assert latest.next_retry_at is None

    @pytest.mark.parametrize(
        ("reason", "document_type", "bound"),
        [
            ("unreadable", "unreadable", None),
            ("pages", "over_bounds", "pages"),
            ("size", "over_bounds", "size"),
            ("memory", "over_bounds", "memory"),
            ("text", "over_bounds", "text"),
            ("crash", "over_bounds", "crash"),
        ],
    )
    def test_a_read_over_a_bound_is_a_rejected_revision(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        monkeypatch: pytest.MonkeyPatch,
        reason: str,
        document_type: str,
        bound: str | None,
    ) -> None:
        from receipt_index import statements

        monkeypatch.setattr(statements, "read_statement", lambda *a, **k: _read(reason))
        replies = Replies()
        assert _run(pg_conn, _raw(make_pdf(_PAGE)), store, replies).status == "success"
        row = _one(
            pg_conn,
            "SELECT status, document_type, rejected_bound"
            " FROM receipt.statement_revisions",
        )
        assert (row["status"], row["document_type"], row["rejected_bound"]) == (
            "rejected",
            document_type,
            bound,
        )
        assert replies.calls == []

    @pytest.mark.parametrize(
        ("status", "error_class"), [(503, "transient"), (401, "config")]
    )
    def test_a_provider_error_stops_the_source_and_writes_nothing(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        status: int,
        error_class: str,
    ) -> None:
        from pydantic_ai.exceptions import ModelHTTPError

        def fail() -> dict[str, Any]:
            raise ModelHTTPError(status, "test-model")

        raw = _raw(make_pdf(_PAGE))
        outcome = _run(pg_conn, raw, store, Replies(account=fail))
        assert (outcome.status, outcome.error_class) == ("failed", error_class)
        assert outcome.stops_source
        assert _one(pg_conn, "SELECT count(*) AS n FROM receipt.statements")["n"] == 0
        (latest, *_) = get_item_history(pg_conn, raw.source_id)
        assert latest.status == "failed"

    def test_an_item_with_no_file_content_is_an_item_error(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
    ) -> None:
        import dataclasses

        raw = dataclasses.replace(_raw(b""), raw_bytes=None, file_content=None)
        outcome = _run(pg_conn, raw, store, Replies())
        assert (outcome.status, outcome.error_class) == ("failed", "content")

    def test_a_failed_call_logs_no_number_and_no_traceback(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
        store: LocalEvidenceStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        number = "5274910386254173"
        caplog.set_level(logging.DEBUG)
        raw = _raw(make_pdf(_PAGE), name=f"Statement {number}.pdf")
        replies = Replies(
            account={**_ACCOUNT, "institution": f"Bank {number}", "last4": "x"}
        )
        assert _run(pg_conn, raw, store, replies).status == "failed"
        windows = {number[i : i + 8] for i in range(len(number) - 7)}
        assert caplog.records
        for record in caplog.records:
            text = record.getMessage()
            assert not any(w in text for w in windows), (record.name, text[:200])
            if record.name.startswith("receipt_index"):
                assert record.exc_info is None, record.name
