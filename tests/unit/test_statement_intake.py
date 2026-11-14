"""Statement intake through `ingest` and `serve` (plan U6).

A Drive source with ``documents: statements`` feeds only the statement
pipeline, and a receipt source never reaches it. Every name and number here
is synthetic.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from receipt_index import pipeline, service, statements
from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.cli import cli
from receipt_index.config import AppConfig, GdriveSourceConfig, ImapSourceConfig
from receipt_index.fetching import (
    STATEMENT_INGEST_TYPE,
    SourceDownError,
    SourcePool,
    fetch_item,
)
from receipt_index.models import ItemRef, RawReceipt, SourceRead
from receipt_index.pipeline import IngestResult, ItemOutcome, run_ingest
from receipt_index.service import ItemClock, Job, JobQueue, Service, Worker
from tests.unit.test_cli import _make_config
from tests.unit.test_pipeline import (
    FakeAdapter,
    FakeRepo,
    _log,
    _past,
    _store,
    history,
    repo,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = ["history", "repo"]  # fixtures, imported for pytest

# A synthetic card number in a file name, as a dropped statement can carry.
_NUMBER = "4929381766207319"
_NAME = f"Statement_{_NUMBER}.pdf"
_SOURCE = "bank-statements"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _drive(name: str, folder: str, documents: str = "receipts") -> dict[str, Any]:
    return {
        "name": name,
        "type": "gdrive",
        "folder_id": folder,
        "token_json": "{}",
        "documents": documents,
    }


def _imap(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "imap",
        "host": "mail.example.com",
        "username": "user@example.com",
        "password": "x",  # pragma: allowlist secret
    }


def _app(*sources: dict[str, Any]) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sources": list(sources),
            "database": {"url": "postgresql://localhost/x"},
            "llm": {"api_key": "x"},  # pragma: allowlist secret
        }
    )


class TestConfig:
    def test_a_drive_source_takes_receipts_by_default(self) -> None:
        config = _app(
            {k: v for k, v in _drive("scans", "f1").items() if k != "documents"}
        )
        (source,) = config.sources
        assert isinstance(source, GdriveSourceConfig)
        assert source.documents == "receipts"

    def test_a_statement_source_on_its_own_folder_is_accepted(self) -> None:
        config = _app(
            _drive("scans", "f1"), _drive(_SOURCE, "f2", "statements"), _imap("mail")
        )
        assert [getattr(s, "documents", None) for s in config.sources] == [
            "receipts",
            "statements",
            None,
        ]

    def test_a_statement_source_on_a_receipt_folder_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="same Drive folder as receipt"):
            _app(_drive("scans", "f1"), _drive(_SOURCE, "f1", "statements"))

    def test_the_overlap_is_refused_whichever_comes_first(self) -> None:
        with pytest.raises(ValidationError, match="same Drive folder"):
            _app(_drive(_SOURCE, "f1", "statements"), _drive("scans", "f1"))

    def test_two_statement_sources_on_one_folder_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="same Drive folder as statement"):
            _app(
                _drive(_SOURCE, "f1", "statements"),
                _drive("more-statements", "f1", "statements"),
            )

    @pytest.mark.parametrize("variant", ["abc/", " abc", "abc /", "\tabc//"])
    def test_a_folder_id_variant_still_overlaps(self, variant: str) -> None:
        with pytest.raises(ValidationError, match="same Drive folder as receipt"):
            _app(_drive("scans", "abc"), _drive(_SOURCE, variant, "statements"))

    def test_a_folder_id_is_kept_without_whitespace_or_a_trailing_slash(
        self,
    ) -> None:
        (source,) = _app(_drive("scans", " abc/ ")).sources
        assert isinstance(source, GdriveSourceConfig)
        assert source.folder_id == "abc"

    def test_two_receipt_sources_on_one_folder_stay_accepted(self) -> None:
        config = _app(_drive("scans", "abc"), _drive("more-scans", "abc/"))
        assert len(config.sources) == 2

    def test_two_sources_with_one_name_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="two sources are named 'mail'"):
            _app(_imap("mail"), _drive("mail", "f2", "statements"))

    def test_an_unknown_documents_value_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _app(_drive("scans", "f1", "invoices"))


# ---------------------------------------------------------------------------
# Dispatch: run_ingest and handle_item
# ---------------------------------------------------------------------------


def _ref(file_id: str, *, skip: str | None = None) -> ItemRef:
    # A Drive item's subject is its file name.
    return ItemRef(
        source_id=file_id,
        source_type="gdrive",
        subject=_NAME,
        file_name=_NAME,
        drive_meta={"id": file_id, "mimeType": "application/zip" if skip else "pdf"},
        skip_reason=skip,
    )


def _read(*refs: ItemRef) -> SourceRead:
    return SourceRead(
        items=list(refs),
        drive_page_token="tok-new",
        drive_account_id="acct-1",
    )


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Which pipeline each item reached."""
    seen: dict[str, list[Any]] = {"receipt": [], "statement": []}

    def receipt(_conn: Any, raw: RawReceipt, **kwargs: Any) -> ItemOutcome:
        seen["receipt"].append((raw, kwargs))
        return ItemOutcome("skipped")

    def statement(_conn: Any, raw: RawReceipt, **kwargs: Any) -> ItemOutcome:
        seen["statement"].append((raw, kwargs))
        return ItemOutcome("success")

    monkeypatch.setattr(pipeline, "process_item", receipt)
    monkeypatch.setattr(statements, "process_statement", statement)
    return seen


def _ingest(adapter: FakeAdapter, **kwargs: Any) -> IngestResult:
    return run_ingest(
        conn=MagicMock(),
        adapter=adapter,  # type: ignore[arg-type]
        store=_store(),
        source_name=_SOURCE,
        source_type="gdrive",
        llm_model="test-model",
        **kwargs,
    )


class TestDispatch:
    def test_a_statement_source_never_reaches_the_receipt_process_item(
        self, repo: FakeRepo, dispatched: dict[str, list[Any]]
    ) -> None:
        agents = MagicMock()
        control = MagicMock()
        control.should_stop.return_value = False
        result = _ingest(
            FakeAdapter(_read(_ref("f-1"), _ref("f-2")), source_type="gdrive"),
            item_type=STATEMENT_INGEST_TYPE,
            statement_agents=agents,
            control=control,
        )
        assert dispatched["receipt"] == []
        assert [raw.source_id for raw, _ in dispatched["statement"]] == ["f-1", "f-2"]
        for _raw, kwargs in dispatched["statement"]:
            assert kwargs["agents"] is agents
            assert kwargs["control"] is control
            assert kwargs["source_name"] == _SOURCE
        assert result.processed == 2
        # The items are logged under the statement type; the cursor moved.
        assert {k["source_type"] for k in repo.row_kwargs} == {STATEMENT_INGEST_TYPE}
        assert repo.drive_saves[-1] == ("tok-new", "acct-1", False)

    def test_a_receipt_source_never_reaches_the_statement_pipeline(
        self, repo: FakeRepo, dispatched: dict[str, list[Any]]
    ) -> None:
        _ingest(
            FakeAdapter(_read(_ref("f-1")), source_type="gdrive"),
            statement_agents=MagicMock(),
        )
        assert dispatched["statement"] == []
        assert [raw.source_id for raw, _ in dispatched["receipt"]] == ["f-1"]
        assert {k["source_type"] for k in repo.row_kwargs} == {"gdrive"}

    def test_a_statement_source_without_its_agents_is_refused(
        self, repo: FakeRepo, dispatched: dict[str, list[Any]]
    ) -> None:
        with pytest.raises(ValueError, match="needs the statement agents"):
            _ingest(
                FakeAdapter(_read(_ref("f-1")), source_type="gdrive"),
                item_type=STATEMENT_INGEST_TYPE,
            )
        assert repo.rows == []

    def test_a_dry_run_of_a_statement_source_needs_no_agents(
        self, repo: FakeRepo, dispatched: dict[str, list[Any]]
    ) -> None:
        result = _ingest(
            FakeAdapter(_read(_ref("f-1")), source_type="gdrive"),
            item_type=STATEMENT_INGEST_TYPE,
            dry_run=True,
        )
        assert result.skipped == 1
        assert repo.rows == []

    def test_a_statement_items_rows_carry_no_subject_and_no_sender(
        self, repo: FakeRepo, dispatched: dict[str, list[Any]]
    ) -> None:
        _ingest(
            FakeAdapter(_read(_ref("f-1")), source_type="gdrive"),
            item_type=STATEMENT_INGEST_TYPE,
            statement_agents=MagicMock(),
        )
        (started,) = repo.row_kwargs
        assert started["status"] == "started"
        assert started["email_subject"] is None
        assert started["email_sender"] is None

    def test_a_skipped_statement_item_keeps_only_a_masked_name(
        self, repo: FakeRepo, dispatched: dict[str, list[Any]]
    ) -> None:
        _ingest(
            FakeAdapter(_read(_ref("f-1", skip="unsupported file type"))),
            item_type=STATEMENT_INGEST_TYPE,
            statement_agents=MagicMock(),
        )
        (skipped,) = repo.row_kwargs
        assert skipped["status"] == "skipped"
        assert skipped["email_subject"] is None
        assert _NUMBER not in str(skipped)
        assert "Statement_[number].pdf" in skipped["error_message"]

    def test_a_skipped_receipt_item_keeps_its_name(
        self, repo: FakeRepo, dispatched: dict[str, list[Any]]
    ) -> None:
        _ingest(FakeAdapter(_read(_ref("f-1", skip="unsupported file type"))))
        (skipped,) = repo.row_kwargs
        assert _NAME in skipped["error_message"]

    def test_a_statement_fetch_error_is_kept_by_its_class(
        self,
        repo: FakeRepo,
        dispatched: dict[str, list[Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class Failing(FakeAdapter):
            def fetch(self, ref: ItemRef) -> RawReceipt | None:
                raise ValueError(f"cannot read {_NAME}")

        _ingest(
            Failing(_read(_ref("f-1"))),
            item_type=STATEMENT_INGEST_TYPE,
            statement_agents=MagicMock(),
        )
        _started, failed = repo.row_kwargs
        assert failed["status"] == "failed"
        assert failed["error_message"] == "ValueError"
        assert failed["email_subject"] is None
        assert _NUMBER not in caplog.text
        assert dispatched["statement"] == []

    def test_a_statement_source_that_goes_down_is_kept_by_its_class(
        self, repo: FakeRepo, dispatched: dict[str, list[Any]]
    ) -> None:
        class Down(FakeAdapter):
            def fetch(self, ref: ItemRef) -> RawReceipt | None:
                raise SourceUnavailableError(f"Drive refused {_NAME}")

        result = _ingest(
            Down(_read(_ref("f-1"))),
            item_type=STATEMENT_INGEST_TYPE,
            statement_agents=MagicMock(),
        )
        assert result.source_error == "SourceUnavailableError"
        assert repo.polls[-1] == "SourceUnavailableError"


# ---------------------------------------------------------------------------
# The shared retry loop
# ---------------------------------------------------------------------------


class _ByIdAdapter:
    def __init__(self, raw: RawReceipt | None = None, error: Exception | None = None):
        self.raw = raw
        self.error = error

    def __enter__(self) -> _ByIdAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def fetch_by_id(self, source_id: str) -> RawReceipt | None:
        if self.error is not None:
            raise self.error
        return self.raw


def _statement_raw(source_id: str) -> RawReceipt:
    return RawReceipt(
        source_id=source_id,
        source_name=_SOURCE,
        source_type="gdrive",
        date=datetime(2027, 2, 3, tzinfo=UTC),
        subject=_NAME,
        file_name=_NAME,
        file_content=b"%PDF-1.4 synthetic",
        raw_bytes=b"%PDF-1.4 synthetic",
    )


def _statement_config() -> GdriveSourceConfig:
    return GdriveSourceConfig(
        name=_SOURCE, folder_id="f2", token_json="{}", documents="statements"
    )


def _statement_retry(adapter: _ByIdAdapter, due: list[Any]) -> pipeline.RetryResult:
    with ExitStack() as stack:
        pool = SourcePool([_statement_config()], lambda _c: adapter, stack)  # type: ignore[arg-type, return-value]
        return pipeline.run_due_retries(
            MagicMock(),
            due,
            pool=pool,
            store=_store(),
            agents={},
            llm_model="test-model",
            own_source=_SOURCE,
            statement_agents=MagicMock(),
        )


def _due_statement(sid: str, status: str = "failed") -> Any:
    # An older row that still holds a subject and a sender (from before U6)
    # must not have them copied forward.
    return _log(
        sid,
        status,
        source_type=STATEMENT_INGEST_TYPE,
        source_name=_SOURCE,
        email_subject=_NAME,
        email_sender="someone@example.com",
        attempt=1,
        counts_attempt=True,
        error_class="content",
        next_retry_at=_past(),
    )


class TestRetryLoop:
    def test_a_statement_retrys_rows_carry_no_subject_and_no_sender(
        self,
        repo: FakeRepo,
        history: dict[str, list[Any]],
        dispatched: dict[str, list[Any]],
    ) -> None:
        sid = "f-1"
        history[sid] = [_due_statement(sid)]
        result = _statement_retry(_ByIdAdapter(_statement_raw(sid)), [history[sid][0]])
        assert result.processed == 1
        (started,) = repo.row_kwargs
        assert started["status"] == "started"
        assert started["email_subject"] is None
        assert started["email_sender"] is None
        assert len(dispatched["statement"]) == 1

    def test_a_failed_statement_retrys_rows_carry_no_subject(
        self,
        repo: FakeRepo,
        history: dict[str, list[Any]],
        dispatched: dict[str, list[Any]],
    ) -> None:
        sid = "f-1"
        history[sid] = [_due_statement(sid)]
        _statement_retry(_ByIdAdapter(None), [history[sid][0]])
        started, failed = repo.row_kwargs
        assert failed["status"] == "failed"
        for row in (started, failed):
            assert row["email_subject"] is None
            assert row["email_sender"] is None

    def test_a_dead_statement_items_closing_row_carries_no_subject(
        self,
        repo: FakeRepo,
        history: dict[str, list[Any]],
        dispatched: dict[str, list[Any]],
    ) -> None:
        sid = "f-1"
        history[sid] = [_due_statement(sid, "started")]
        _statement_retry(_ByIdAdapter(None), [history[sid][0]])
        (failed,) = repo.row_kwargs
        assert failed["status"] == "failed"
        assert failed["email_subject"] is None
        assert failed["email_sender"] is None

    def test_a_statement_source_down_on_retry_is_kept_by_its_class(
        self,
        repo: FakeRepo,
        history: dict[str, list[Any]],
        dispatched: dict[str, list[Any]],
    ) -> None:
        sid = "f-1"
        history[sid] = [_due_statement(sid)]
        error = SourceUnavailableError(f"Drive refused {_NAME}")
        result = _statement_retry(_ByIdAdapter(error=error), [history[sid][0]])
        assert result.stopped == f"source {_SOURCE} unavailable: SourceUnavailableError"
        assert _NUMBER not in result.stopped
        assert repo.row_kwargs == []


class TestFetchItem:
    def test_a_receipt_sources_down_text_keeps_the_adapters_reason(self) -> None:
        config = GdriveSourceConfig(name="scans", folder_id="f1", token_json="{}")
        adapter = _ByIdAdapter(error=SourceUnavailableError("quota exceeded"))
        with ExitStack() as stack:
            pool = SourcePool([config], lambda _c: adapter, stack)  # type: ignore[arg-type, return-value]
            with pytest.raises(SourceDownError, match="quota exceeded"):
                fetch_item(
                    pool, source_id="f-1", source_name="scans", source_type="gdrive"
                )


# ---------------------------------------------------------------------------
# serve: the worker and the service
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    calls = {
        "run_ingest": MagicMock(return_value=IngestResult()),
        "select_due": MagicMock(return_value=[MagicMock()]),
        "run_due_retries": MagicMock(return_value=pipeline.RetryResult()),
    }
    for name, mock in calls.items():
        monkeypatch.setattr(service, name, mock)
    return calls


def _worker(sources: list[Any], statement_agents: Any = None) -> Worker:
    return Worker(
        sources=sources,
        locks={s.name: MagicMock() for s in sources},
        queue=JobQueue(),
        stop=threading.Event(),
        clock=ItemClock(),
        open_conn=lambda: MagicMock(closed=False, broken=False),
        adapter_factory=lambda cfg: MagicMock(name=f"adapter-{cfg.name}"),
        agents={"email": MagicMock(), "document": MagicMock()},
        store=MagicMock(),
        llm_model="test-model",
        policy=MagicMock(),
        configured=[s.name for s in sources],
        statement_agents=statement_agents,
    )


class TestServe:
    def test_a_statement_sources_poll_goes_to_the_statement_pipeline(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        agents = MagicMock()
        worker = _worker([_statement_config()], agents)
        worker.run_job(Job("poll", _SOURCE))
        kwargs = pipeline_calls["run_ingest"].call_args.kwargs
        # The sync state keeps gdrive; the items go as statements.
        assert kwargs["source_type"] == "gdrive"
        assert kwargs["item_type"] == STATEMENT_INGEST_TYPE
        assert kwargs["statement_agents"] is agents

    def test_a_receipt_sources_poll_stays_a_receipt_poll(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        scans = GdriveSourceConfig(name="scans", folder_id="f1", token_json="{}")
        worker = _worker([scans], MagicMock())
        worker.run_job(Job("poll", "scans"))
        kwargs = pipeline_calls["run_ingest"].call_args.kwargs
        assert kwargs["item_type"] == "gdrive"

    def test_a_statement_sources_retries_select_statement_items(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        agents = MagicMock()
        worker = _worker([_statement_config()], agents)
        worker.run_job(Job("retry", _SOURCE))
        assert pipeline_calls["select_due"].call_args.kwargs["source_type"] == (
            STATEMENT_INGEST_TYPE
        )
        retry = pipeline_calls["run_due_retries"].call_args.kwargs
        assert retry["statement_agents"] is agents

    def test_serve_refuses_a_statement_source_without_its_agents(self) -> None:
        with pytest.raises(ValueError, match=f"statement source\\(s\\) {_SOURCE}"):
            Service(
                sources=[_statement_config()],
                all_sources=[_statement_config()],
                locks={_SOURCE: MagicMock()},
                timings=MagicMock(),
                open_conn=MagicMock(),
                adapter_factory=MagicMock(),
                agents={},
                store=MagicMock(),
                llm_model="test-model",
                policy=MagicMock(),
            )


# ---------------------------------------------------------------------------
# The CLI: one-shot ingest, serve and backfill
# ---------------------------------------------------------------------------


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("unused: the config is patched\n")
    return path


def _statement_app() -> AppConfig:
    config = _make_config()
    return config.model_copy(update={"sources": [_statement_config()]})


@pytest.fixture
def cli_ingest() -> Iterator[dict[str, MagicMock]]:
    from tests.unit.test_cli import FakeSourceLock

    FakeSourceLock.held = set()
    FakeSourceLock.acquired = []
    mocks: dict[str, MagicMock] = {}
    with ExitStack() as stack:
        for key, target in {
            "load": "receipt_index.cli.load_config",
            "run": "receipt_index.pipeline.run_ingest",
            "due": "receipt_index.repository.select_due",
            "retries": "receipt_index.pipeline.run_due_retries",
            "agents": "receipt_index.cli._statement_agents",
            "extraction": "receipt_index.extraction.create_extraction_agent",
            "conn": "receipt_index.db.get_connection",
            "store": "receipt_index.store.open_store",
            "drive": "receipt_index.adapters.gdrive.GdriveAdapter",
        }.items():
            mocks[key] = stack.enter_context(patch(target))
        stack.enter_context(patch("receipt_index.locks.SourceLock", FakeSourceLock))
        mocks["load"].return_value = _statement_app()
        mocks["run"].return_value = IngestResult(processed=1)
        mocks["due"].return_value = [MagicMock()]
        mocks["retries"].return_value = pipeline.RetryResult()
        yield mocks


class TestOneShotIngest:
    def test_a_statement_source_is_routed_by_its_ingest_type(
        self, cli_ingest: dict[str, MagicMock], config_file: Path
    ) -> None:
        result = CliRunner().invoke(cli, ["--config", str(config_file), "ingest"])
        assert result.exit_code == 0, result.output
        agents = cli_ingest["agents"].return_value
        run = cli_ingest["run"].call_args.kwargs
        assert run["source_type"] == "gdrive"
        assert run["item_type"] == STATEMENT_INGEST_TYPE
        assert run["statement_agents"] is agents
        due = cli_ingest["due"].call_args.kwargs
        assert due["source_type"] == STATEMENT_INGEST_TYPE
        retry = cli_ingest["retries"].call_args.kwargs
        assert retry["statement_agents"] is agents

    def test_a_dry_run_builds_no_statement_agents(
        self, cli_ingest: dict[str, MagicMock], config_file: Path
    ) -> None:
        result = CliRunner().invoke(
            cli, ["--config", str(config_file), "ingest", "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        cli_ingest["agents"].assert_not_called()
        assert cli_ingest["run"].call_args.kwargs["statement_agents"] is None


class TestStatementAgentsFactory:
    def test_none_without_a_statement_source(self) -> None:
        from receipt_index.cli import _statement_agents

        config = _make_config()
        assert _statement_agents(config, config.sources) is None

    def test_built_through_the_guarded_constructors(self) -> None:
        from receipt_index.cli import _statement_agents

        config = _statement_app()
        model, settings = MagicMock(), MagicMock()
        with (
            patch(
                "receipt_index.llm.anthropic_model", return_value=(model, settings)
            ) as build,
            patch("receipt_index.statements.StatementAgents.create") as create,
        ):
            built = _statement_agents(config, config.sources)
        assert built is create.return_value
        # A model object from llm.anthropic_model (no tracing), never a name.
        build.assert_called_once_with(
            api_key=config.llm.api_key, model=config.llm.model
        )
        create.assert_called_once_with(model, settings)


class TestServeCommand:
    def test_serve_passes_statement_agents_for_a_held_statement_source(
        self, config_file: Path
    ) -> None:
        from tests.unit.test_cli import FakeSourceLock

        FakeSourceLock.held = set()
        FakeSourceLock.acquired = []
        with (
            patch("receipt_index.cli.load_config", return_value=_statement_app()),
            patch("receipt_index.locks.SourceLock", FakeSourceLock),
            patch("receipt_index.cli._statement_agents") as build,
            patch("receipt_index.extraction.create_extraction_agent"),
            patch("receipt_index.store.open_store"),
            patch("receipt_index.service.Service") as service_cls,
        ):
            service_cls.return_value.shutdown.return_value = True
            result = CliRunner().invoke(cli, ["--config", str(config_file), "serve"])
        assert result.exit_code == 0, result.output
        held = build.call_args.args[1]
        assert [s.name for s in held] == [_SOURCE]
        kwargs = service_cls.call_args.kwargs
        assert kwargs["statement_agents"] is build.return_value

    def test_a_statement_source_it_does_not_hold_builds_no_agents(
        self, config_file: Path
    ) -> None:
        """Another serve watches the statement source (review, U6).

        This serve holds only a receipt source, so it builds no statement
        agents and doesn't refuse to start over a source it doesn't watch.
        """
        from tests.unit.test_cli import FakeSourceLock

        statement = _statement_config()
        receipts = statement.model_copy(
            update={
                "name": "scans",
                "folder_id": "receipts-folder",
                "documents": "receipts",
            }
        )
        config = _make_config().model_copy(update={"sources": [receipts, statement]})
        FakeSourceLock.held = {_SOURCE}
        FakeSourceLock.acquired = []

        def no_agents(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("statement agents built for a source not held")

        try:
            with (
                patch("receipt_index.cli.load_config", return_value=config),
                patch("receipt_index.locks.SourceLock", FakeSourceLock),
                patch("receipt_index.llm.anthropic_model", no_agents),
                patch("receipt_index.extraction.create_extraction_agent"),
                patch("receipt_index.store.open_store"),
                patch("receipt_index.service.Service") as service_cls,
            ):
                service_cls.return_value.shutdown.return_value = True
                result = CliRunner().invoke(
                    cli, ["--config", str(config_file), "serve"]
                )
        finally:
            FakeSourceLock.held = set()
        assert result.exit_code == 0, result.output
        assert FakeSourceLock.acquired == ["scans"]
        kwargs = service_cls.call_args.kwargs
        assert [s.name for s in kwargs["sources"]] == ["scans"]
        assert kwargs["statement_agents"] is None


class TestBackfill:
    def test_backfill_refuses_a_statement_source(self, config_file: Path) -> None:
        with patch("receipt_index.cli.load_config", return_value=_statement_app()):
            result = CliRunner().invoke(
                cli,
                [
                    "--config",
                    str(config_file),
                    "backfill",
                    "--source",
                    _SOURCE,
                    "--since",
                    "2027-01-01",
                ],
            )
        assert result.exit_code == 1
        assert "takes statements; backfill reads receipt sources only" in (
            result.output
        )


def test_imap_sources_are_unchanged() -> None:
    source = ImapSourceConfig(
        name="mail",
        host="mail.example.com",
        username="user@example.com",
        password="x",  # pragma: allowlist secret
    )
    assert not hasattr(source, "documents")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def _collect(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        retries: bool = True,
        by_source: dict[str, dict[str, int]] | None = None,
    ) -> Any:
        from contextlib import nullcontext

        from receipt_index import repository
        from receipt_index.health import collect

        if by_source is None:
            by_source = {
                _SOURCE: {"complete": 3, "incomplete": 0, "held": 1, "rejected": 2},
                "gone": {"complete": 9, "incomplete": 0, "held": 0, "rejected": 0},
            }
        failures: list[str] = []

        def count(_c: Any, source_name: str) -> dict[str, int]:
            failures.append(source_name)
            return {"pending": 6, "retrying": 4, "terminal": 5}

        monkeypatch.setattr(repository, "list_sync_state", lambda _c: [])
        monkeypatch.setattr(repository, "count_due_retries", lambda _c: 0)
        monkeypatch.setattr(repository, "count_ingest_failures", lambda *_a, **_k: 0)
        monkeypatch.setattr(repository, "count_statement_failures", count)
        monkeypatch.setattr(
            repository, "count_statements_by_source", lambda _c: by_source
        )
        config = _app(
            _drive("scans", "f1"), _drive(_SOURCE, "f2", "statements"), _imap("mail")
        )
        report = collect(
            config,
            lambda: nullcontext(MagicMock()),  # type: ignore[arg-type, return-value]
            heartbeat_path=tmp_path / "none",
            retries=retries,
        )
        return report, failures

    def test_a_statement_source_has_its_own_counts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from receipt_index.health import report_json, report_text

        report, failures = self._collect(monkeypatch, tmp_path)
        data = report_json(report)
        by_name = {s["name"]: s for s in data["sources"]}
        assert by_name[_SOURCE]["documents"] == "statements"
        assert by_name[_SOURCE]["statements"] == {
            "complete": 3,
            "incomplete": 0,
            "held": 1,
            "rejected": 2,
            "pending": 6,
            "retrying": 4,
            "terminal": 5,
        }
        # Its retry counts are its own, not every source's.
        assert failures == [_SOURCE]
        assert by_name["scans"]["documents"] == "receipts"
        assert by_name["scans"]["statements"] is None
        assert by_name["mail"]["statements"] is None
        text = report_text(report)
        assert f"  {_SOURCE} (gdrive, statements, f2)" in text
        assert (
            "    statements: complete 3, incomplete 0, held 1, rejected 2, "
            "pending 6, retrying 4, terminal 5"
        ) in text
        assert "  scans (gdrive, f1)" in text

    def test_a_statement_source_with_none_yet_counts_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        report, _ = self._collect(monkeypatch, tmp_path, by_source={})
        (statement,) = [s for s in report.sources if s.name == _SOURCE]
        assert statement.statements == {
            "complete": 0,
            "incomplete": 0,
            "held": 0,
            "rejected": 0,
            "pending": 6,
            "retrying": 4,
            "terminal": 5,
        }

    def test_the_ping_reads_no_statement_counts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        report, failures = self._collect(monkeypatch, tmp_path, retries=False)
        (statement,) = [s for s in report.sources if s.name == _SOURCE]
        assert statement.statements is None
        assert failures == []
