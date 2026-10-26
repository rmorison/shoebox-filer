"""Source adapter protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from types import TracebackType

    from receipt_index.models import ItemRef, RawReceipt, SourceRead, SyncState


@runtime_checkable
class SourceAdapter(Protocol):
    """Protocol for receipt source adapters.

    A read happens in two steps, so the pipeline can skip a known item
    before its body is downloaded:

    - ``list_new(state)`` lists what is new since the stored sync state, as
      refs that carry only headers or metadata, plus the cursor candidates.
    - ``fetch(ref)`` downloads one item, or returns None if it has gone.

    An adapter is a context manager, so a connection can be held across
    both steps; ``close()`` releases it.
    """

    source_type: str

    @property
    def folder(self) -> str:
        """The IMAP folder or Drive folder ID the sync-state row is keyed on."""
        ...

    def list_new(self, state: SyncState | None) -> SourceRead: ...

    def fetch(self, ref: ItemRef) -> RawReceipt | None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
