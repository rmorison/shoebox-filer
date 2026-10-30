"""Failure classes for ingest errors (always-on ingest design, section 6).

Every failed ``ingest_log`` row carries one of three classes:

- ``transient``: the service, not the item, failed (an overloaded or
  rate-limited LLM, a 5xx, a dropped connection or a timeout, a source that
  can't be reached, a full disk, a statement timeout). Retrying later should
  work, so it uses up no attempt.
- ``config``: a source-wide refusal that the operator fixes once (an LLM key
  that is rejected, credits that ran out). Every item would fail the same
  way, so it uses up no attempt either.
- ``content``: anything else, which is the item's own problem (a parse or
  render error, an extraction that fails validation, a model refusal). It
  uses up an attempt.

The class is decided from the exception and its ``__cause__``/``__context__``
chain, so the boundaries that raise them stay unchanged.
"""

from __future__ import annotations

import errno
from typing import Literal

ErrorClass = Literal["transient", "content", "config"]

# How far down a cause chain to look; chains are short in practice.
_MAX_CHAIN = 10

# A full disk or an exhausted quota fails every item the same way until the
# operator frees space, so it uses up no attempt.
_DISK_FULL = frozenset({errno.ENOSPC, errno.EDQUOT})


class TransientError(Exception):
    """Raised by a boundary to say that a failure is the service's, not the item's."""


def _transient_types() -> tuple[type[BaseException], ...]:
    from psycopg.errors import QueryCanceled
    from pydantic_ai.exceptions import (
        ModelConnectionError,
        ModelRateLimitError,
        ModelRequestTimeout,
        ModelServerError,
        ModelUnavailableError,
    )

    from receipt_index.adapters.base import SourceUnavailableError

    return (
        TransientError,
        QueryCanceled,  # a statement_timeout
        SourceUnavailableError,
        ModelRateLimitError,
        ModelUnavailableError,  # ModelOverloadedError is a subclass
        ModelServerError,
        ModelConnectionError,  # ModelTimeoutError is a subclass
        ModelRequestTimeout,
        ConnectionError,
        TimeoutError,
    )


def _config_types() -> tuple[type[BaseException], ...]:
    from pydantic_ai.exceptions import (
        ModelAuthenticationError,
        ModelNotFoundError,
        ModelQuotaExceededError,
    )

    # A model the API doesn't know (a retired or mistyped llm.model) fails
    # every item the same way, like a rejected key.
    return (ModelAuthenticationError, ModelNotFoundError, ModelQuotaExceededError)


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < _MAX_CHAIN:
        if any(current is s for s in seen):
            break
        seen.append(current)
        # Follow the chain a traceback would show: ``raise ... from None``
        # hides the context.
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    return seen


def classify(exc: BaseException) -> ErrorClass:
    """Return the failure class of ``exc``.

    A model refusal (``ExtractionRefusedError``, or pydantic-ai's
    ``ContentFilterError``, anywhere in the chain) is always ``content``.
    Otherwise a config error outranks a transient one found in the same
    chain, because waiting won't fix it.
    """
    from pydantic_ai.exceptions import ContentFilterError

    from receipt_index.extraction import ExtractionRefusedError

    chain = _chain(exc)
    # A model refusal is the item's: asking again gets the same answer.
    refusal = (ExtractionRefusedError, ContentFilterError)
    if any(isinstance(e, refusal) for e in chain):
        return "content"
    config = _config_types()
    if any(isinstance(e, config) or _http_status(e) in (401, 403, 404) for e in chain):
        return "config"
    transient = _transient_types()
    if any(
        isinstance(e, transient) or _http_transient(e) or _disk_full(e) for e in chain
    ):
        return "transient"
    return "content"


def _http_status(exc: BaseException) -> int | None:
    """The status of a ``ModelHTTPError`` that no category subclass covers."""
    from pydantic_ai.exceptions import ModelHTTPError

    return exc.status_code if isinstance(exc, ModelHTTPError) else None


def _disk_full(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in _DISK_FULL


def _http_transient(exc: BaseException) -> bool:
    status = _http_status(exc)
    return status is not None and (status in (408, 429) or status >= 500)
