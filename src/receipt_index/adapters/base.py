"""Source adapter protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator

    from receipt_index.models import RawReceipt


@runtime_checkable
class SourceAdapter(Protocol):
    """Protocol for receipt source adapters."""

    def fetch_unprocessed(self, processed_ids: set[str]) -> Iterator[RawReceipt]: ...
