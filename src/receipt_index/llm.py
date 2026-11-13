"""The Anthropic model the extraction agents run on."""

from __future__ import annotations

import logging
from typing import Final, Literal

import pydantic_ai
from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider

# pydantic-ai prints a first-run banner to the console. It carries no
# payload, but it is noise in a service's container logs.
pydantic_ai.BANNER_ENABLED = False

DEFAULT_MODEL = "claude-haiku-5-5"

# Haiku 5.5 thinks adaptively, and effort sets how much. Its default is
# medium, and production has run extraction at that default, so pinning
# medium keeps today's quality while a provider default can't move it.
# Going lower should wait until it is measured on real receipts.
EXTRACTION_EFFORT: Final[Literal["medium"]] = "medium"

# A deadline on each model request, below `item_timeout_seconds`, so a hung
# call raises ModelRequestTimeout (transient) instead of holding the item.
LLM_REQUEST_TIMEOUT_SECONDS: Final = 180.0


def anthropic_model(
    *, api_key: str, model: str
) -> tuple[AnthropicModel, AnthropicModelSettings]:
    """The model, using ``api_key``, and the settings to run it with.

    The key goes to the provider, so ``ANTHROPIC_API_KEY`` isn't consulted.
    The effort is set only on models that take one: an older model rejects it.
    No sampling parameters are set; Haiku 5.5 rejects non-default ones.
    """
    # The SDK traces every request with OpenTelemetry by default, following
    # the global tracer provider and ANTHROPIC_OPEN_TELEMETRY_* variables; a
    # span can carry the request (a statement's text) and the reply. Off,
    # whatever the environment says, and no trace headers sent either.
    client = AsyncAnthropic(
        api_key=api_key, open_telemetry={"traces": False, "propagation": False}
    )
    llm = AnthropicModel(model, provider=AnthropicProvider(anthropic_client=client))
    settings = AnthropicModelSettings(request_timeout=LLM_REQUEST_TIMEOUT_SECONDS)
    if llm.profile.get("anthropic_supports_effort", False):
        settings["anthropic_effort"] = EXTRACTION_EFFORT
    return llm, settings


# Loggers that print a whole request or response at DEBUG: the anthropic
# SDK logs each request's options, which hold the document text (a
# statement's account numbers) and, on a retry, the model's earlier output;
# the HTTP stack under it (httpx, httpcore, and their 2.x forks httpx2 and
# httpcore2) logs connection and stream events. Every top-level logger whose
# name starts with one of these prefixes is clamped, so a renamed or forked
# package is caught too; the known names are clamped even before import.
PAYLOAD_LOGGER_PREFIXES: Final = ("anthropic", "httpx", "httpcore")
PAYLOAD_LOGGERS: Final = ("anthropic", "httpx", "httpx2", "httpcore", "httpcore2")


def _payload_logger_names() -> set[str]:
    """The known names, and every logger already made under the prefixes.

    Children are included: a child given its own level (say
    ``anthropic._base_client`` set to DEBUG by hand) would otherwise log
    below its clamped parent.
    """
    registry = logging.root.manager.loggerDict
    under = [n for n in registry if n.split(".")[0].startswith(PAYLOAD_LOGGER_PREFIXES)]
    # Every top-level name, even one only a placeholder for its children so
    # far, and every child that is a real logger (it may have its own level).
    tops = {n.split(".")[0] for n in under}
    made = {n for n in under if isinstance(registry[n], logging.Logger)}
    return set(PAYLOAD_LOGGERS) | tops | made


def quiet_payload_loggers() -> None:
    """Hold the payload loggers at INFO or above, whatever the app's level.

    Called when logging is configured, whenever an agent is built (by
    which time the HTTP stack is imported) and at the start of every
    statement run, so neither the app's DEBUG level nor
    ``ANTHROPIC_LOG=debug`` can print a request. Every top-level logger
    under the prefixes is clamped, which covers its children; a child that
    was given its own level below INFO is clamped too (a decision:
    statements carry account numbers). A level set by hand between two
    calls holds until the next one: code that lowers a payload logger
    during a statement run is clamped at the next run, not at once. So
    nothing in this process may set one below INFO.
    """
    for name in _payload_logger_names():
        logger = logging.getLogger(name)
        top = "." not in name
        if (top and logger.level < logging.INFO) or (
            not top and logging.NOTSET < logger.level < logging.INFO
        ):
            logger.setLevel(logging.INFO)
