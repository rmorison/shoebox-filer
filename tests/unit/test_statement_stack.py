"""Statement calls through the locked pydantic-ai and anthropic packages.

The other statement tests stub the model with a FunctionModel, which never
runs pydantic-ai's Anthropic model: its request building, its streaming, or
its mapping of the API's stop reasons. These run each call unmocked against
the stub Messages API in ``conftest.py`` (see docs/solutions/test-failures/
tests-that-mock-the-agent-hid-two-breaks-in-the-locked-llm-stack.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from receipt_index import statement_extraction as se
from receipt_index.llm import anthropic_model
from tests.unit.conftest import STUB_INPUT_TOKENS, STUB_OUTPUT_TOKENS

if TYPE_CHECKING:
    from tests.unit.conftest import Reply, StubAnthropic

_MODEL = "claude-haiku-5-5"
_KEY = "stub-key-passed-in"
_PAGES = ("Synthetic Savings Bank\nChecking 1234", "Jan 5 Synthetic purchase -10.00")
_LISTED = se.ListedAccount(
    label="Checking ending 1234", account_type="deposit", first_page=1, last_page=2
)
_ACCOUNT: dict[str, Any] = {
    "institution": "Synthetic Savings Bank",
    "last4": "1234",
    "currency": "USD",
    "beginning_balance": "100.00",
    "ending_balance": "90.00",
    "line_order": "oldest_first",
    "lines": [
        {
            "posted": {"month": 1, "day": 5, "year": 2027},
            "description": "Synthetic purchase",
            "amount": "-10.00",
            "running_balance": "90.00",
            "page": 2,
        }
    ],
}
_FIRST_PASS: dict[str, Any] = {
    "document_type": "deposit",
    "accounts": [
        {
            "label": "Checking ending 1234",
            "account_type": "deposit",
            "first_page": 1,
            "last_page": 2,
        }
    ],
}


def _account_agent() -> Any:
    llm, settings = anthropic_model(api_key=_KEY, model=_MODEL)
    return se.create_account_agent("deposit", llm, settings)


def _first_pass_agent() -> Any:
    llm, settings = anthropic_model(api_key=_KEY, model=_MODEL)
    return se.create_first_pass_agent(llm, settings)


_ONE_CALL = se.CallUsage(
    input_tokens=STUB_INPUT_TOKENS, output_tokens=STUB_OUTPUT_TOKENS, requests=1
)


def test_an_account_call_through_the_locked_stack(stub_api: StubAnthropic) -> None:
    stub_api.tool_input = _ACCOUNT
    run = se.run_account(_account_agent(), _PAGES, _LISTED, None)
    assert not run.capped
    assert isinstance(run.output, se.AccountExtraction)
    assert run.output.lines[0].amount == "-10.00"
    assert run.usage == _ONE_CALL
    request = stub_api.requests[0]
    assert request["max_tokens"] == se.ACCOUNT_OUTPUT_TOKENS
    assert stub_api.api_keys == [_KEY]


def test_a_first_pass_through_the_locked_stack(stub_api: StubAnthropic) -> None:
    stub_api.tool_input = _FIRST_PASS
    run = se.run_first_pass(_first_pass_agent(), _PAGES)
    assert run.output is not None
    assert run.output.accounts[0].label == "Checking ending 1234"
    assert stub_api.requests[0]["max_tokens"] == se.FIRST_PASS_OUTPUT_TOKENS


@pytest.mark.parametrize("call", ["first_pass", "account"])
def test_a_max_tokens_stop_through_the_locked_stack_is_a_cap(
    stub_api: StubAnthropic, call: str
) -> None:
    stub_api.tool_input = _FIRST_PASS if call == "first_pass" else _ACCOUNT
    stub_api.stop_reason = "max_tokens"
    if call == "first_pass":
        first = se.run_first_pass(_first_pass_agent(), _PAGES)
        assert (first.capped, first.output, first.usage) == (True, None, _ONE_CALL)
    else:
        run = se.run_account(_account_agent(), _PAGES, _LISTED, None)
        assert (run.capped, run.output, run.usage) == (True, None, _ONE_CALL)
    assert len(stub_api.requests) == 1


def test_a_context_window_stop_through_the_locked_stack_is_an_error(
    stub_api: StubAnthropic,
) -> None:
    stub_api.tool_input = _ACCOUNT
    stub_api.stop_reason = "model_context_window_exceeded"
    with pytest.raises(se.StatementCallError) as raised:
        se.run_account(_account_agent(), _PAGES, _LISTED, None)
    assert raised.value.kind == "context_window"
    assert raised.value.usage == _ONE_CALL


@pytest.mark.parametrize("reply", ["refusal", "partial_refusal"])
def test_a_refusal_through_the_locked_stack_is_refused(
    stub_api: StubAnthropic, reply: Reply
) -> None:
    # A refusal, after some text or none, ends the run with no retry. The
    # locked pydantic-ai does this itself; the agents also carry the
    # content-filter capability, as the receipt agent does.
    stub_api.reply = reply
    with pytest.raises(se.StatementCallError) as raised:
        se.run_account(_account_agent(), _PAGES, _LISTED, None)
    assert (raised.value.kind, raised.value.error_class) == ("refused", "content")
    assert len(stub_api.requests) == 1


def test_a_failing_call_logs_no_model_output(
    stub_api: StubAnthropic, caplog: pytest.LogCaptureFixture
) -> None:
    # An invalid output that quotes a full card number. Every record any
    # library logs during the call (pydantic-ai, anthropic, httpx), at any
    # level, must hold no run of eight or more digits.
    import logging
    import re
    import traceback

    stub_api.tool_input = {
        **_ACCOUNT,
        "institution": "Bank 4111111111113456",
        "last4": "not-four-digits",
    }
    # The app at DEBUG, as `logging.level: DEBUG` sets it; the payload
    # loggers stay clamped (llm.quiet_payload_loggers).
    caplog.set_level(logging.DEBUG)
    agent = _account_agent()
    with pytest.raises(se.StatementCallError) as raised:
        se.run_account(agent, _PAGES, _LISTED, None)
    assert raised.value.kind == "schema"
    assert caplog.records, "nothing was logged, so nothing was checked"
    long_run = re.compile(r"[0-9]{8,}")
    for record in caplog.records:
        text = record.getMessage()
        if record.exc_info:
            text += "".join(traceback.format_exception(*record.exc_info))
        assert not long_run.search(text), (record.name, text[:200])


@pytest.mark.parametrize("build", ["receipt", "first_pass", "account"])
def test_every_agent_clamps_the_payload_loggers(build: str) -> None:
    # Without relying on the CLI: building any agent holds the loggers that
    # print a whole request at INFO or above.
    import logging

    from receipt_index.extraction import create_extraction_agent
    from receipt_index.llm import PAYLOAD_LOGGERS

    saved = {name: logging.getLogger(name).level for name in PAYLOAD_LOGGERS}
    try:
        for name in PAYLOAD_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        if build == "receipt":
            create_extraction_agent(api_key=_KEY, model=_MODEL)
        elif build == "first_pass":
            _first_pass_agent()
        else:
            _account_agent()
        for name in PAYLOAD_LOGGERS:
            assert not logging.getLogger(name).isEnabledFor(logging.DEBUG), name
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
