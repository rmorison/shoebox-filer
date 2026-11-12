"""The Anthropic model the extraction agents run on."""

from __future__ import annotations

import logging
from typing import Final, Literal

from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider

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
    llm = AnthropicModel(model, provider=AnthropicProvider(api_key=api_key))
    settings = AnthropicModelSettings(request_timeout=LLM_REQUEST_TIMEOUT_SECONDS)
    if llm.profile.get("anthropic_supports_effort", False):
        settings["anthropic_effort"] = EXTRACTION_EFFORT
    return llm, settings


# Loggers that print a whole request or response at DEBUG: the anthropic
# SDK logs each request's options, which hold the document text (a
# statement's account numbers) and, on a retry, the model's earlier output.
PAYLOAD_LOGGERS: Final = ("anthropic", "httpx", "httpcore")


def quiet_payload_loggers() -> None:
    """Hold the payload loggers at INFO or above, whatever the app's level.

    Called when logging is configured and when a statement agent is built,
    so neither the app's DEBUG level nor ``ANTHROPIC_LOG=debug`` can print
    a request. Debugging the SDK takes raising these loggers by hand.
    """
    for name in PAYLOAD_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level < logging.INFO:  # NOTSET or DEBUG
            logger.setLevel(logging.INFO)
