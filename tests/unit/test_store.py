"""Tests for receipt_index.store."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from receipt_index.store import LocalFileStore

if TYPE_CHECKING:
    from pathlib import Path


class TestLocalFileStore:
    """Tests for LocalFileStore."""

    def test_save_creates_year_month_directories(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        store.save(date(2025, 6, 15), "Amazon", Decimal("42.99"), b"pdf-data")

        assert (store_root / "2025" / "06").is_dir()

    def test_save_returns_relative_path(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        result = store.save(date(2025, 6, 15), "Amazon", Decimal("42.99"), b"pdf-data")

        assert result == "2025/06/2025-06-15__amazon__42.99.pdf"

    def test_save_writes_file_content(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        pdf_data = b"%PDF-1.4 fake content"
        result = store.save(date(2025, 1, 5), "Costco", Decimal("157.32"), pdf_data)

        full_path = store_root / result
        assert full_path.read_bytes() == pdf_data

    def test_filename_format(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        result = store.save(
            date(2025, 12, 25), "Home Depot", Decimal("1250.00"), b"data"
        )

        assert result == "2025/12/2025-12-25__home-depot__1250.00.pdf"

    def test_vendor_slugification(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)

        # Spaces and special chars
        result = store.save(date(2025, 1, 1), "Trader Joe's", Decimal("50.00"), b"data")
        assert "trader-joe" in result

        # Already clean
        result2 = store.save(date(2025, 1, 1), "amazon", Decimal("10.00"), b"data")
        assert "amazon" in result2

    def test_vendor_slug_max_length(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        long_vendor = (
            "A Very Long Vendor Name That Exceeds The Maximum Length Allowed For Slugs"
        )
        result = store.save(date(2025, 1, 1), long_vendor, Decimal("10.00"), b"data")

        # Extract slug from filename
        filename = result.split("/")[-1]
        # Format: YYYY-MM-DD__slug__amount.pdf
        slug = filename.split("__")[1]
        assert len(slug) <= 50

    def test_duplicate_handling(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        d = date(2025, 6, 15)

        path1 = store.save(d, "Amazon", Decimal("42.99"), b"first")
        path2 = store.save(d, "Amazon", Decimal("42.99"), b"second")

        assert path1 != path2
        assert path1 == "2025/06/2025-06-15__amazon__42.99.pdf"
        assert path2 == "2025/06/2025-06-15__amazon__42.99_2.pdf"

        # Verify both files exist with correct content
        assert (store_root / path1).read_bytes() == b"first"
        assert (store_root / path2).read_bytes() == b"second"

    def test_triple_duplicate(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        d = date(2025, 6, 15)

        store.save(d, "Amazon", Decimal("42.99"), b"1")
        store.save(d, "Amazon", Decimal("42.99"), b"2")
        path3 = store.save(d, "Amazon", Decimal("42.99"), b"3")

        assert path3 == "2025/06/2025-06-15__amazon__42.99_3.pdf"

    def test_get_path(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        relative = "2025/06/2025-06-15__amazon__42.99.pdf"

        result = store.get_path(relative)
        assert result == store_root / relative

    def test_exists_true(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        relative = store.save(date(2025, 1, 1), "Test", Decimal("1.00"), b"data")

        assert store.exists(relative) is True

    def test_exists_false(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)

        assert store.exists("nonexistent/path.pdf") is False

    def test_month_zero_padded(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        result = store.save(date(2025, 1, 5), "Test", Decimal("1.00"), b"data")

        assert result.startswith("2025/01/")

    def test_december(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        result = store.save(date(2025, 12, 31), "Test", Decimal("1.00"), b"data")

        assert result.startswith("2025/12/")

    def test_vendor_path_traversal_sanitized(self, store_root: Path) -> None:
        store = LocalFileStore(store_root)
        result = store.save(
            date(2025, 1, 1), "../../etc/passwd", Decimal("1.00"), b"data"
        )

        assert ".." not in result
        assert result.startswith("2025/01/")
        # File must be inside store root
        full_path = store.get_path(result)
        assert str(full_path).startswith(str(store_root))
