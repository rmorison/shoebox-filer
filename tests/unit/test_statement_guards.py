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
import textwrap
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
# The module check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "opentelemetry.instrumentation",
        "opentelemetry.instrumentation.httpx",
        "opentelemetry.instrumentation.anything_new",
        "logfire._internal.integrations.httpx",
        "logfire._internal.integrations.llm_providers.anthropic",
        "logfire._internal.integrations.pydantic_ai",
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
        "logfire._internal.integrations",
        "logfire._internal.integrations.llm_providers",
        "opentelemetry.sdk.trace",
        # A prefix match only on the dotted package, never a longer name.
        "opentelemetry.instrumentationish",
    ],
)
def test_modules_a_plain_import_loads_pass(name: str) -> None:
    refuse_instrumentation([name])


def _run(script: str) -> subprocess.CompletedProcess[str]:
    """``script`` in a fresh interpreter, with nothing sent anywhere."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LOGFIRE_", "OTEL_"))
    }
    env["LOGFIRE_SEND_TO_LOGFIRE"] = "false"
    env["LOGFIRE_CONSOLE"] = "false"
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


_VERDICT = """
from receipt_index.statements import (
    InstrumentationLoadedError,
    refuse_instrumentation,
)
try:
    refuse_instrumentation()
except InstrumentationLoadedError:
    print("REFUSED")
else:
    print("PASSED")
"""


def test_a_plain_import_of_pydantic_ai_and_logfire_passes() -> None:
    imports = """
        import sys
        import logfire
        import pydantic_ai
        import receipt_index.llm
        import receipt_index.statements
        assert "logfire" in sys.modules
        """
    result = _run(textwrap.dedent(imports) + _VERDICT)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "PASSED"


# Each instrument call run for real. The logfire module names the check
# looks for are private: if a release renames the module a call loads, the
# check stops seeing it and this test fails.
_INSTRUMENT_CALLS = {
    "logfire.instrument_httpx": "logfire.instrument_httpx()",
    "logfire.instrument_anthropic": "logfire.instrument_anthropic()",
    "logfire.instrument_pydantic_ai": "logfire.instrument_pydantic_ai()",
    "an opentelemetry instrumentor": (
        "from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor\n"
        "HTTPXClientInstrumentor().instrument()"
    ),
}


@pytest.mark.parametrize("call", sorted(_INSTRUMENT_CALLS))
def test_each_instrument_call_is_refused(call: str) -> None:
    result = _run(
        "import logfire\n"
        "import pydantic_ai\n"
        "logfire.configure(send_to_logfire=False, console=False)\n"
        + _INSTRUMENT_CALLS[call]
        + "\n"
        + textwrap.dedent(_VERDICT)
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "REFUSED"


def test_the_logfire_calls_are_refused_by_their_own_modules() -> None:
    """Not only through an OpenTelemetry instrumentor they also load.

    ``instrument_httpx`` loads ``opentelemetry.instrumentation`` too, so a
    rename of its logfire module would hide behind that. This shows each
    logfire module the check names is the one its call loads.
    """
    result = _run(
        """
        import sys
        import logfire
        import pydantic_ai
        from receipt_index import statements
        logfire.configure(send_to_logfire=False, console=False)
        before = set(sys.modules)
        logfire.instrument_httpx()
        logfire.instrument_anthropic()
        logfire.instrument_pydantic_ai()
        loaded = set(sys.modules) - before
        missing = sorted(statements._PATCHING_MODULES - loaded)
        print("MISSING", missing)
        """
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "MISSING []"


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
