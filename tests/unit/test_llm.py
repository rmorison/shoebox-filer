"""Tests for receipt_index.llm."""

from __future__ import annotations

from receipt_index.llm import (
    EXTRACTION_EFFORT,
    LLM_REQUEST_TIMEOUT_SECONDS,
    anthropic_model,
)


def test_key_goes_to_the_provider() -> None:
    llm, _ = anthropic_model(api_key="passed-in", model="claude-haiku-5-5")

    assert llm.client.api_key == "passed-in"  # pragma: allowlist secret


def test_effort_set_on_haiku_5_5() -> None:
    _, settings = anthropic_model(api_key="k", model="claude-haiku-5-5")

    assert settings == {
        "anthropic_effort": EXTRACTION_EFFORT,
        "request_timeout": LLM_REQUEST_TIMEOUT_SECONDS,
    }


def test_no_effort_on_a_model_without_it() -> None:
    # A config can still name Haiku 4.5, which rejects the effort setting.
    _, settings = anthropic_model(api_key="k", model="claude-haiku-4-5-20251001")

    assert settings == {"request_timeout": LLM_REQUEST_TIMEOUT_SECONDS}


def test_requests_have_a_deadline() -> None:
    _, settings = anthropic_model(api_key="k", model="claude-haiku-5-5")

    assert settings["request_timeout"] == LLM_REQUEST_TIMEOUT_SECONDS


def test_a_request_past_its_deadline_raises_a_transient_error() -> None:
    import asyncio

    from pydantic_ai import Agent
    from pydantic_ai.exceptions import ModelRequestTimeout
    from pydantic_ai.models.function import FunctionModel

    from receipt_index.errors import classify

    async def slow(messages: object, info: object) -> object:
        await asyncio.sleep(5)
        raise AssertionError("the deadline should have cut this short")

    agent = Agent(FunctionModel(slow), model_settings={"request_timeout": 0.1})
    try:
        agent.run_sync("hello")
    except ModelRequestTimeout as exc:
        assert classify(exc) == "transient"
    else:
        raise AssertionError("expected ModelRequestTimeout")
