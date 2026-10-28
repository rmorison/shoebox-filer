"""Tests for receipt_index.errors: the failure class of each error."""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelHTTPError,
    ModelNotFoundError,
    ModelOverloadedError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestRejectedError,
    ModelRequestTimeout,
    ModelServerError,
    ModelTimeoutError,
    ModelUnavailableError,
    UnexpectedModelBehavior,
)

from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.errors import TransientError, classify
from receipt_index.extraction import ExtractionRefusedError

_M = "claude-test"


@pytest.mark.parametrize(
    "exc",
    [
        ModelOverloadedError(_M, "overloaded"),
        ModelRateLimitError(_M, "rate limited"),
        ModelUnavailableError(_M, "unavailable"),
        ModelServerError(_M, "internal error"),
        ModelConnectionError(_M, "connection reset"),
        ModelTimeoutError(_M, "read timed out"),
        ModelRequestTimeout(_M, 60.0),
        ModelHTTPError(529, _M),
        ModelHTTPError(503, _M),
        ModelHTTPError(429, _M),
        SourceUnavailableError("IMAP connection to mail.example.com failed"),
        TransientError("store backend down"),
        ConnectionResetError("reset by peer"),
        TimeoutError("timed out"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_transient(exc: Exception) -> None:
    assert classify(exc) == "transient"


@pytest.mark.parametrize(
    "exc",
    [
        ModelAuthenticationError(_M, "invalid x-api-key"),
        ModelQuotaExceededError(_M, "Your credit balance is too low"),
        ModelHTTPError(401, _M),
        ModelHTTPError(403, _M),
        ModelNotFoundError(_M, "model: claude-retired"),
        ModelHTTPError(404, _M),
    ],
    ids=lambda e: type(e).__name__,
)
def test_config(exc: Exception) -> None:
    assert classify(exc) == "config"


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("cannot parse message"),
        UnexpectedModelBehavior("output validation failed"),
        ModelRequestRejectedError(_M, "prompt is too long"),
        ModelHTTPError(400, _M),
        LookupError("item is no longer in the source"),
        RuntimeError("render failed"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_content(exc: Exception) -> None:
    assert classify(exc) == "content"


def _raised_from(outer: Exception, inner: Exception) -> Exception:
    try:
        try:
            raise inner
        except Exception as exc:
            raise outer from exc
    except Exception as caught:
        return caught


class RefusedError(Exception):
    """A wrapper the refusal might be raised from."""


class TestRefusals:
    def test_extraction_refusal_is_content(self) -> None:
        assert classify(ExtractionRefusedError("cyber", "declined")) == "content"

    def test_extraction_refusal_from_the_filter_error(self) -> None:
        exc = _raised_from(
            ExtractionRefusedError(None, None), ContentFilterError("refusal")
        )
        assert classify(exc) == "content"

    def test_refusal_wrapping_a_connection_error_is_still_content(self) -> None:
        # Refusals outrank transient causes anywhere in the chain.
        exc = _raised_from(
            ExtractionRefusedError("cyber", "declined"), ConnectionResetError()
        )
        assert classify(exc) == "content"


class TestChains:
    def test_cause_decides_for_a_wrapper(self) -> None:
        exc = _raised_from(
            RuntimeError("extraction failed"), ModelOverloadedError(_M, "x")
        )
        assert classify(exc) == "transient"

    def test_refusal_is_content(self) -> None:
        exc = _raised_from(RefusedError("declined"), ContentFilterError("refusal"))
        assert classify(exc) == "content"

    def test_refusal_outranks_a_transient_in_the_chain(self) -> None:
        inner = _raised_from(ContentFilterError("refusal"), ConnectionResetError())
        assert classify(_raised_from(RefusedError("declined"), inner)) == "content"

    def test_config_outranks_transient(self) -> None:
        exc = _raised_from(
            ModelAuthenticationError(_M, "bad key"), ConnectionResetError("reset")
        )
        assert classify(exc) == "config"

    def test_context_is_followed(self) -> None:
        try:
            try:
                raise ModelRateLimitError(_M, "slow down")
            except Exception:
                raise RuntimeError("while handling") from None
        except RuntimeError as exc:
            # ``from None`` hides the context: nothing marks it transient.
            assert classify(exc) == "content"
        try:
            try:
                raise ModelRateLimitError(_M, "slow down")
            except Exception:
                raise RuntimeError("while handling")  # noqa: B904
        except RuntimeError as exc:
            assert classify(exc) == "transient"
