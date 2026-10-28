"""The Anthropic model the extraction agents run on."""

from __future__ import annotations

from typing import Final, Literal

from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider

DEFAULT_MODEL = "claude-haiku-5-5"

# Haiku 5.5 thinks adaptively, and effort sets how much. Its default is
# medium, and production has run extraction at that default, so pinning
# medium keeps today's quality while a provider default can't move it.
# Going lower should wait until it is measured on real receipts.
EXTRACTION_EFFORT: Final[Literal["medium"]] = "medium"


def anthropic_model(
    *, api_key: str, model: str
) -> tuple[AnthropicModel, AnthropicModelSettings]:
    """The model, using ``api_key``, and the settings to run it with.

    The key goes to the provider, so ``ANTHROPIC_API_KEY`` isn't consulted.
    The effort is set only on models that take one: an older model rejects it.
    No sampling parameters are set; Haiku 5.5 rejects non-default ones.
    """
    llm = AnthropicModel(model, provider=AnthropicProvider(api_key=api_key))
    settings = AnthropicModelSettings()
    if llm.profile.get("anthropic_supports_effort", False):
        settings["anthropic_effort"] = EXTRACTION_EFFORT
    return llm, settings
