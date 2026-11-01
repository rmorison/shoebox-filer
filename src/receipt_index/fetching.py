"""The one path that re-fetches an item by its source ID.

Retries, ``retry`` and re-extraction all use it (always-on ingest design,
section 6, "Re-fetching an item"). An item is looked up in the source it
came from; a row that doesn't name a source still configured is tried
against every configured source of its type.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from receipt_index.adapters.base import SourceUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import ExitStack

    from receipt_index.adapters.base import SourceAdapter
    from receipt_index.config import SourceConfig
    from receipt_index.models import RawReceipt

logger = logging.getLogger(__name__)


class SourceDownError(Exception):
    """The item's source can't be reached in this run."""


class SourcePool:
    """The configured sources' adapters, opened on first use.

    A source that raised ``SourceUnavailableError`` is not contacted again
    in the same run.
    """

    def __init__(
        self,
        configs: list[SourceConfig],
        factory: Callable[[SourceConfig], SourceAdapter],
        stack: ExitStack,
    ) -> None:
        self._configs = {c.name: c for c in configs}
        self._factory = factory
        self._stack = stack
        self._adapters: dict[str, SourceAdapter] = {}
        self.unavailable: dict[str, str] = {}

    def candidates(self, source_name: str | None, source_type: str) -> list[str]:
        """The sources to try for an item.

        Its own source when it is still configured; otherwise every configured
        source of its type, as the ingest design re-fetches a row whose source
        isn't known.
        """
        if source_name in self._configs:
            return [source_name]
        return [
            name for name, config in self._configs.items() if config.type == source_type
        ]

    def config(self, name: str) -> SourceConfig | None:
        """A configured source's settings, without opening its adapter."""
        return self._configs.get(name)

    def adapter(self, name: str) -> SourceAdapter:
        if name not in self._adapters:
            adapter = self._factory(self._configs[name])
            self._adapters[name] = self._stack.enter_context(adapter)
        return self._adapters[name]


@dataclass
class Fetched:
    """What a re-fetch found.

    ``raw`` is the item, from source ``source_name``; None when no candidate
    had it. ``item_error`` is the last error a candidate raised for the item
    itself (refused, unparseable), if any.
    """

    raw: RawReceipt | None
    source_name: str | None = None
    item_error: Exception | None = None


def fetch_item(
    pool: SourcePool,
    *,
    source_id: str,
    source_name: str | None,
    source_type: str,
) -> Fetched:
    """Find one item by its source ID in its candidate sources.

    Raises ``SourceDownError`` when a candidate source can't be reached (it
    is then marked unavailable for the rest of the run). An error with the
    item itself moves on to the next candidate and is returned in
    ``item_error`` if no candidate has the item.
    """
    item_error: Exception | None = None
    for name in pool.candidates(source_name, source_type):
        if name in pool.unavailable:
            raise SourceDownError(pool.unavailable[name])
        try:
            raw = pool.adapter(name).fetch_by_id(source_id)
        except SourceUnavailableError as exc:
            logger.warning("Source %s is unavailable; not contacting it again", name)
            pool.unavailable[name] = f"source {name} unavailable: {exc}"
            raise SourceDownError(pool.unavailable[name]) from exc
        except Exception as exc:
            logger.warning(
                "Re-fetch of %s from %s failed", source_id, name, exc_info=True
            )
            item_error = exc
            continue
        if raw is not None:
            return Fetched(raw, name)
    return Fetched(None, item_error=item_error)
