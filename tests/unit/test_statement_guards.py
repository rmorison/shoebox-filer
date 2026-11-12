"""Guards around statement work (plan U5): tracing, instrumentation, gates.

The statement agents are built only through the guarded constructors, so a
model that could trace is refused; statement work is refused while tracing
instrumentation is loaded; and any error from the gates is an item error
kept by its class alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from receipt_index import statements
from receipt_index.llm import anthropic_model
from receipt_index.pdf_reader import StatementRead
from receipt_index.statement_extraction import CallUsage, FirstPass, FirstPassRun
from receipt_index.statement_gates import GateInputError
from receipt_index.statements import (
    InstrumentationLoadedError,
    StatementAgents,
    StatementItemError,
    refuse_instrumentation,
)

_MODEL = "claude-haiku-5-5"
_KEY = "stub-key-passed-in"
_CARD = "4111111111111111"

# ---------------------------------------------------------------------------
# StatementAgents.create: only the guarded constructors
# ---------------------------------------------------------------------------


def test_create_refuses_a_model_name() -> None:
    with pytest.raises(TypeError):
        StatementAgents.create(_MODEL)  # type: ignore[arg-type]


def test_create_refuses_an_anthropic_model_with_a_default_client() -> None:
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    default_client = AnthropicModel(
        _MODEL,
        provider=AnthropicProvider(anthropic_client=AsyncAnthropic(api_key=_KEY)),
    )
    with pytest.raises(ValueError, match="tracing off"):
        StatementAgents.create(default_client)


def test_create_builds_every_agent_untraced_from_the_factory_model() -> None:
    llm, settings = anthropic_model(api_key=_KEY, model=_MODEL)
    agents = StatementAgents.create(llm, settings)
    assert set(agents.accounts) == {"deposit", "card", "loan"}
    for agent in (agents.first_pass, *agents.accounts.values()):
        assert agent.instrument is False


def test_create_refuses_while_instrumentation_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic_ai.models.test import TestModel

    name = "opentelemetry.instrumentation.planted"
    monkeypatch.setitem(sys.modules, name, ModuleType(name))
    with pytest.raises(InstrumentationLoadedError, match="planted"):
        StatementAgents.create(TestModel())


# ---------------------------------------------------------------------------
# The module check: fail closed on logfire's integrations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "opentelemetry.instrumentation",
        "opentelemetry.instrumentation.httpx",
        "opentelemetry.instrumentation.anything_new",
        "logfire._internal.integrations.httpx",
        "logfire._internal.integrations.llm_providers.anthropic",
        "logfire._internal.integrations.llm_providers.openai",
        "logfire._internal.integrations.pydantic_ai",
        "logfire._internal.integrations.mcp",
        "logfire._internal.integrations.pytest",
        # One no release has yet: fail closed.
        "logfire._internal.integrations.anything_new",
        "logfire.integrations.anything_new",
    ],
)
def test_a_loaded_instrumentation_module_is_refused(name: str) -> None:
    with pytest.raises(InstrumentationLoadedError) as raised:
        refuse_instrumentation(["logfire", "pydantic_ai", name])
    assert name in str(raised.value)


@pytest.mark.parametrize(
    "name",
    [
        # pydantic-ai's logfire extra imports these in every process.
        "logfire",
        "logfire_api",
        *sorted(statements._LOGFIRE_BASELINE),
        "opentelemetry.sdk.trace",
        # A prefix match only on the dotted package, never a longer name.
        "opentelemetry.instrumentationish",
    ],
)
def test_modules_the_app_loads_pass(name: str) -> None:
    refuse_instrumentation([name])


def _run(
    script: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """``script`` in a fresh interpreter, with nothing sent anywhere."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LOGFIRE_", "OTEL_", "PYDANTIC_"))
    }
    env["LOGFIRE_SEND_TO_LOGFIRE"] = "false"
    env["LOGFIRE_CONSOLE"] = "false"
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip().splitlines()[-1]


_VERDICT = """
from receipt_index.statements import (
    InstrumentationLoadedError,
    refuse_instrumentation,
)
try:
    refuse_instrumentation()
except InstrumentationLoadedError as exc:
    print("REFUSED", exc)
else:
    print("PASSED")
"""

# The whole app imported: every module of the package.
_IMPORT_APP = """
import importlib
import pkgutil
import sys

import receipt_index
from receipt_index.statements import _LOGFIRE_INTEGRATIONS

for info in pkgutil.walk_packages(receipt_index.__path__, "receipt_index."):
    importlib.import_module(info.name)
assert "logfire" in sys.modules
"""
# The statement entry point: the agents built as serve builds them (which
# runs the check itself, and raises if it refuses).
_WHOLE_APP = (
    _IMPORT_APP
    + """
from receipt_index.llm import anthropic_model
from receipt_index.statements import StatementAgents

llm, settings = anthropic_model(api_key="stub-key", model="claude-haiku-5-5")
StatementAgents.create(llm, settings)
loaded = sorted(
    name
    for name in sys.modules
    if name.startswith(_LOGFIRE_INTEGRATIONS)
)
print("LOADED", loaded)
"""
)


def test_the_whole_app_at_its_statement_entry_point_passes() -> None:
    """The control. A logfire upgrade that widens the baseline fails here."""
    result = _run(_WHOLE_APP + _VERDICT)
    assert _last_line(result) == "PASSED"
    loaded = next(
        line for line in result.stdout.splitlines() if line.startswith("LOADED")
    )
    assert loaded == f"LOADED {sorted(statements._LOGFIRE_BASELINE)}"


# Each instrument call run for real, after the whole app is imported. The
# logfire module names are private: if a release changes what a call
# loads, the check must still see it, or this test fails.
_LOGFIRE_CALLS = (
    "instrument_httpx",
    "instrument_anthropic",
    "instrument_openai",
    "instrument_pydantic_ai",
    "instrument_mcp",
)


def _instrumented(call: str) -> str:
    return (
        _WHOLE_APP
        + "import logfire\n"
        + "logfire.configure(send_to_logfire=False, console=False)\n"
        + "before = set(sys.modules)\n"
        + call
        + "\n"
        + "from receipt_index.statements import instrumentation_modules\n"
        + "new = sorted(set(sys.modules) - before)\n"
        + "own = [m for m in instrumentation_modules(new)"
        + " if m.startswith(_LOGFIRE_INTEGRATIONS)]\n"
        + "print('OWN', own)\n"
        + _VERDICT
    )


@pytest.mark.parametrize("call", _LOGFIRE_CALLS)
def test_each_logfire_instrument_call_is_refused_by_its_own_module(
    call: str,
) -> None:
    # By a logfire module of its own, not only through an OpenTelemetry
    # instrumentor it may also load (instrument_httpx loads one).
    result = _run(_instrumented(f"logfire.{call}()"))
    assert _last_line(result).startswith("REFUSED")
    own = next(line for line in result.stdout.splitlines() if line.startswith("OWN"))
    assert own != "OWN []", result.stdout


def test_an_opentelemetry_instrumentor_is_refused() -> None:
    result = _run(
        _instrumented(
            "from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor\n"
            "HTTPXClientInstrumentor().instrument()"
        )
    )
    assert _last_line(result).startswith("REFUSED")


# ---------------------------------------------------------------------------
# The state check: pydantic recording, which loads no module
# ---------------------------------------------------------------------------


def test_logfire_instrument_pydantic_is_refused() -> None:
    result = _run(_instrumented("logfire.instrument_pydantic()"))
    assert _last_line(result).startswith("REFUSED")
    assert "pydantic plugin" in _last_line(result)


@pytest.mark.parametrize("configure", [False, True])
def test_the_pydantic_record_variable_alone_is_refused(configure: bool) -> None:
    # No code at all: the variable turns logfire's pydantic plugin on.
    script = _IMPORT_APP
    if configure:
        script += (
            "import logfire\nlogfire.configure(send_to_logfire=False, console=False)\n"
        )
    result = _run(
        script + _VERDICT, env_extra={"LOGFIRE_PYDANTIC_PLUGIN_RECORD": "all"}
    )
    assert _last_line(result).startswith("REFUSED")
    assert "pydantic plugin" in _last_line(result)


@pytest.mark.parametrize(
    "filters",
    [
        # Record only pydantic-ai's validators: the check's own isn't wrapped.
        {"LOGFIRE_PYDANTIC_PLUGIN_INCLUDE": r"pydantic_ai\..*"},
        # Or skip this package's.
        {"LOGFIRE_PYDANTIC_PLUGIN_EXCLUDE": r"receipt_index\..*"},
    ],
)
def test_pydantic_recording_filtered_away_from_the_check_is_refused(
    filters: dict[str, str],
) -> None:
    result = _run(
        _IMPORT_APP + _VERDICT,
        env_extra={"LOGFIRE_PYDANTIC_PLUGIN_RECORD": "all", **filters},
    )
    assert _last_line(result).startswith("REFUSED")
    assert "pydantic plugin" in _last_line(result)


def test_a_logfire_plugin_setting_that_cant_be_read_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A logfire release that renames the setting fails closed.
    monkeypatch.setitem(
        sys.modules, statements._LOGFIRE_PYDANTIC_PLUGIN, ModuleType("renamed")
    )
    with pytest.raises(InstrumentationLoadedError, match="pydantic plugin"):
        refuse_instrumentation([])


def test_a_logfire_plugin_set_to_off_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = ModuleType("plugin")
    plugin.get_pydantic_plugin_config = lambda: SimpleNamespace(record="off")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, statements._LOGFIRE_PYDANTIC_PLUGIN, plugin)
    refuse_instrumentation([])
    plugin.get_pydantic_plugin_config = lambda: SimpleNamespace(record="failure")  # type: ignore[attr-defined]
    with pytest.raises(InstrumentationLoadedError):
        refuse_instrumentation([])


def test_the_statement_models_tell_the_logfire_plugin_not_to_record() -> None:
    from receipt_index import statement_extraction as se

    for model in (se.FirstPass, se.AccountExtraction, se.LoanExtraction):
        assert model.model_config.get("plugin_settings") == {
            "logfire": {"record": "off"}
        }


def test_the_state_check_passes_in_this_process() -> None:
    assert not statements._pydantic_records()


# ---------------------------------------------------------------------------
# serve: the check and the mask warm-up at start
# ---------------------------------------------------------------------------


def _service(statement_agents: Any) -> Any:
    from unittest.mock import MagicMock

    from receipt_index import service
    from receipt_index.config import GdriveSourceConfig

    sources = [GdriveSourceConfig(name="scans", folder_id="folder-1", token_json="{}")]
    return service.Service(
        sources=sources,
        all_sources=sources,
        locks={"scans": MagicMock()},
        timings=service.ServeTimings(
            idle_reissue_seconds=60,
            imap_poll_seconds=3600,
            drive_poll_seconds=3600,
            item_timeout_seconds=1200,
            join_seconds=5,
        ),
        open_conn=lambda: MagicMock(closed=False, broken=False),
        adapter_factory=MagicMock(),
        agents={"email": MagicMock(), "document": MagicMock()},
        store=MagicMock(),
        llm_model="test-model",
        policy=MagicMock(),
        exit_fn=MagicMock(),
        statement_agents=statement_agents,
    )


def test_serve_with_statements_refuses_to_start_while_instrumentation_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    name = "logfire._internal.integrations.pydantic_ai"
    monkeypatch.setitem(sys.modules, name, ModuleType(name))
    with pytest.raises(InstrumentationLoadedError):
        _service(MagicMock())


def test_serve_with_statements_warms_the_mask_at_start() -> None:
    from unittest.mock import MagicMock

    from receipt_index import statement_extraction as se

    se._masked_run.cache_clear()
    se._dashes.cache_clear()
    _service(MagicMock())
    assert se._masked_run.cache_info().currsize == 1
    assert se._dashes.cache_info().currsize == 1


def test_serve_without_statements_runs_no_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "opentelemetry.instrumentation.planted"
    monkeypatch.setitem(sys.modules, name, ModuleType(name))
    _service(None)


# ---------------------------------------------------------------------------
# gate_statement: any exception is an item error, kept by its class alone
# ---------------------------------------------------------------------------


def _extract_with_failing_gates(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> StatementItemError:
    first = FirstPass(document_type="deposit")
    monkeypatch.setattr(
        statements,
        "read_statement",
        lambda *_a, **_k: StatementRead(page_texts=("page one",), pages=1),
    )
    monkeypatch.setattr(
        statements,
        "run_first_pass",
        lambda *_a, **_k: FirstPassRun(output=first, capped=False, usage=CallUsage()),
    )

    def gates(*_a: object, **_k: object) -> None:
        raise error

    monkeypatch.setattr(statements, "gate_statement", gates)
    with pytest.raises(StatementItemError) as raised:
        statements.extract_statement(
            b"%PDF",
            agents=SimpleNamespace(first_pass=None, accounts={}),  # type: ignore[arg-type]
            llm_model=_MODEL,
            source_id="item-1",
            control=None,
            usage=statements._Usage(),
        )
    return raised.value


@pytest.mark.parametrize(
    "error",
    [
        # What the gates raise, with a fixed message.
        GateInputError("decimal"),
        # A bug anywhere else, whose message quotes the model's output.
        KeyError(f"Synthetic Bank {_CARD}"),
        ArithmeticError(f"difference {_CARD}"),
    ],
)
def test_any_gate_error_is_an_item_error_kept_by_its_class(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    raised = _extract_with_failing_gates(monkeypatch, error)
    assert str(raised) == (f"the gates refused the statement: {type(error).__name__}")
    assert _CARD not in str(raised)
    assert raised.__cause__ is None
    assert raised.__suppress_context__
    assert statements._error_class(raised) == "content"


# ---------------------------------------------------------------------------
# Every statement run checks first
# ---------------------------------------------------------------------------


def test_a_run_refuses_when_instrumentation_was_loaded_after_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from receipt_index.models import RawReceipt

    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(
        statements, "insert_ingest_log", lambda _conn, **row: logged.append(row)
    )
    looked_up = MagicMock()
    monkeypatch.setattr(statements, "statement_for_sha256", looked_up)
    name = "logfire._internal.integrations.mcp"
    monkeypatch.setitem(sys.modules, name, ModuleType(name))
    raw = RawReceipt(
        source_type="gdrive_statement",
        source_id="item-1",
        source_name="statements-drive",
        date=datetime(2027, 1, 5, tzinfo=UTC),
        raw_bytes=b"%PDF-1.7 synthetic",
    )
    outcome = statements.process_statement(
        MagicMock(),
        raw,
        store=MagicMock(),
        source_name="statements-drive",
        agents=MagicMock(),
        llm_model=_MODEL,
    )
    # The process, not the item: the source stops, no attempt is counted,
    # and nothing of the item was read.
    assert (outcome.status, outcome.error_class) == ("failed", "config")
    assert outcome.stops_source
    looked_up.assert_not_called()
    (row,) = logged
    assert row["status"] == "failed"
    assert row["counts_attempt"] is False
    assert name in row["error_message"]
