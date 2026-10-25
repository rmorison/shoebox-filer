"""File store abstraction and local filesystem implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from slugify import slugify

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal
    from pathlib import Path


class FileStore(Protocol):
    """Protocol for receipt PDF storage backends."""

    def save(
        self, receipt_date: date, vendor: str, amount: Decimal, pdf_data: bytes
    ) -> str: ...

    def get_path(self, relative_path: str) -> Path: ...

    def exists(self, relative_path: str) -> bool: ...


class LocalFileStore:
    """Local filesystem implementation of FileStore.

    Directory layout: {root}/{YYYY}/{MM}/{YYYY-MM-DD}__{vendor}__{amount}.pdf
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def save(
        self, receipt_date: date, vendor: str, amount: Decimal, pdf_data: bytes
    ) -> str:
        """Save PDF and return the relative path from store root."""
        slug = self._slugify_vendor(vendor)
        dir_path = self.root / str(receipt_date.year) / f"{receipt_date.month:02d}"
        dir_path.mkdir(parents=True, exist_ok=True)

        filename = f"{receipt_date.isoformat()}__{slug}__{amount}.pdf"
        file_path = dir_path / filename

        # Handle duplicates by appending numeric suffix
        counter = 1
        while file_path.exists():
            counter += 1
            filename = f"{receipt_date.isoformat()}__{slug}__{amount}_{counter}.pdf"
            file_path = dir_path / filename

        file_path.write_bytes(pdf_data)
        return str(file_path.relative_to(self.root))

    def get_path(self, relative_path: str) -> Path:
        """Return the absolute path for a relative store path."""
        return self.root / relative_path

    def exists(self, relative_path: str) -> bool:
        """Check whether a file exists in the store."""
        return (self.root / relative_path).exists()

    @staticmethod
    def _slugify_vendor(vendor: str) -> str:
        """Convert vendor name to a filesystem-safe slug, max 50 chars."""
        return str(slugify(vendor, max_length=50))
