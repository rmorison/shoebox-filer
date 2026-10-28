"""Tests for receipt_index.llm."""

from __future__ import annotations

from receipt_index.llm import EXTRACTION_EFFORT, anthropic_model


def test_key_goes_to_the_provider() -> None:
    llm, _ = anthropic_model(api_key="passed-in", model="claude-haiku-5-5")

    assert llm.client.api_key == "passed-in"  # pragma: allowlist secret


def test_effort_set_on_haiku_5_5() -> None:
    _, settings = anthropic_model(api_key="k", model="claude-haiku-5-5")

    assert settings == {"anthropic_effort": EXTRACTION_EFFORT}


def test_no_effort_on_a_model_without_it() -> None:
    # A config can still name Haiku 4.5, which rejects the effort setting.
    _, settings = anthropic_model(api_key="k", model="claude-haiku-4-5-20251001")

    assert settings == {}
