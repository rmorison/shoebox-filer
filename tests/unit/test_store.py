"""Tests for receipt_index.store."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from receipt_index.config import StoreConfig
from receipt_index.store import (
    FileStat,
    InvalidRefError,
    LocalEvidenceStore,
    OriginalConflictError,
    OriginalImmutableError,
    StoreFolder,
    open_store,
    pdf_path_for,
    rendition_name,
    rendition_ref,
    store_rendition,
)

_SHA = hashlib.sha256(b"raw message").digest()


def _rendition(
    store: LocalEvidenceStore,
    data: bytes = b"pdf-data",
    receipt_date: date = date(2025, 6, 15),
    vendor: str = "Amazon",
    amount: str = "42.99",
) -> str:
    """Store a rendition; return its pdf_path."""
    stored = store_rendition(
        store,
        data,
        receipt_date=receipt_date,
        vendor=vendor,
        amount=Decimal(amount),
    )
    return pdf_path_for(stored.ref)


def _original(store: LocalEvidenceStore, data: bytes, name: str = "message.eml") -> str:
    stored = store.put(
        data,
        media_type="message/rfc822",
        purpose="original",
        folder=StoreFolder.for_original(date(2026, 10, 14), "mail-receipts", _SHA),
        name=name,
    )
    return stored.ref


def _tmp_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.name.endswith(".tmp")]


class TestRenditionLayout:
    """Renditions keep today's naming, under renditions/YYYY/MM/."""

    def test_returns_relative_path_under_renditions(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        assert _rendition(store) == "renditions/2025/06/2025-06-15__amazon__42.99.pdf"

    def test_writes_file_content(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        pdf_path = _rendition(store, b"%PDF-1.4 fake content")
        assert (store_root / pdf_path).read_bytes() == b"%PDF-1.4 fake content"

    def test_filename_format(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        result = _rendition(
            store,
            receipt_date=date(2025, 12, 25),
            vendor="Home Depot",
            amount="1250.00",
        )
        assert result == "renditions/2025/12/2025-12-25__home-depot__1250.00.pdf"

    def test_month_zero_padded(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        assert _rendition(store, receipt_date=date(2025, 1, 5)).startswith(
            "renditions/2025/01/"
        )

    def test_vendor_slugification(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        assert "trader-joe" in _rendition(store, vendor="Trader Joe's")

    def test_vendor_slug_max_length(self) -> None:
        name = rendition_name(
            date(2025, 1, 1),
            "A Very Long Vendor Name That Exceeds The Maximum Length Allowed For Slugs",
            Decimal("10.00"),
        )
        assert len(name.split("__")[1]) <= 50

    def test_vendor_path_traversal_sanitized(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        result = _rendition(store, vendor="../../etc/passwd")
        assert ".." not in result
        assert store.local_path(rendition_ref(result)).is_relative_to(store.root)

    def test_duplicate_names_get_numeric_suffix(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        first = _rendition(store, b"first")
        second = _rendition(store, b"second")
        third = _rendition(store, b"third")

        assert first == "renditions/2025/06/2025-06-15__amazon__42.99.pdf"
        assert second == "renditions/2025/06/2025-06-15__amazon__42.99_2.pdf"
        assert third == "renditions/2025/06/2025-06-15__amazon__42.99_3.pdf"
        assert (store_root / first).read_bytes() == b"first"
        assert (store_root / second).read_bytes() == b"second"

    def test_same_bytes_twice_still_gets_its_own_file(self, store_root: Path) -> None:
        """Each receipt has its own rendition file, even with equal bytes."""
        store = LocalEvidenceStore(store_root)
        assert _rendition(store, b"same") != _rendition(store, b"same")

    def test_legacy_name_counts_as_taken(self, store_root: Path) -> None:
        """A name in the pre-layout YYYY/MM folder isn't reused."""
        legacy = store_root / "2025" / "06" / "2025-06-15__amazon__42.99.pdf"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy")
        store = LocalEvidenceStore(store_root)

        result = _rendition(store, b"new")

        assert result == "renditions/2025/06/2025-06-15__amazon__42.99_2.pdf"
        assert legacy.read_bytes() == b"legacy"

    def test_records_hash_size_and_media_type(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        stored = store_rendition(
            store,
            b"pdf-bytes",
            receipt_date=date(2025, 6, 15),
            vendor="Amazon",
            amount=Decimal("42.99"),
        )
        assert stored.sha256 == hashlib.sha256(b"pdf-bytes").digest()
        assert stored.byte_size == len(b"pdf-bytes")
        assert stored.media_type == "application/pdf"
        assert stored.id is None

    def test_leaves_no_temp_file(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        _rendition(store)
        _rendition(store)
        _original(store, b"raw")
        assert _tmp_files(store_root) == []

    def test_failed_link_leaves_no_temp_file(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        with (
            patch("receipt_index.store.os.link", side_effect=OSError("no links")),
            pytest.raises(OSError, match="no links"),
        ):
            _rendition(store)
        assert _tmp_files(store_root) == []


class TestOriginals:
    def test_folder_layout(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        ref = _original(store, b"raw")
        assert ref == (
            "local:originals/2026/10/2026-10-14__mail-receipts__"
            f"{_SHA.hex()[:12]}/message.eml"
        )

    def test_folder_uses_received_date_of_a_datetime(self) -> None:
        folder = StoreFolder.for_original(
            datetime(2026, 1, 31, 23, 0, tzinfo=UTC), "Scans", _SHA
        )
        assert folder.segments[:2] == ("2026", "01")
        assert folder.segments[2].startswith("2026-01-31__scans__")

    def test_put_is_idempotent(self, store_root: Path) -> None:
        """The same bytes twice give the same file, and no second write."""
        store = LocalEvidenceStore(store_root)
        first = _original(store, b"raw")
        path = store.local_path(first)
        before = path.stat()

        with patch("receipt_index.store.os.link") as link:
            second = _original(store, b"raw")

        assert second == first
        link.assert_not_called()
        after = path.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert _tmp_files(store_root) == []

    def test_different_bytes_at_the_target_are_refused(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        ref = _original(store, b"raw")

        with pytest.raises(OriginalConflictError):
            _original(store, b"tampered")

        assert store.local_path(ref).read_bytes() == b"raw"

    def test_lost_race_reuses_the_winners_file(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        real_link = os.link

        def winner_first(src: str | Path, dst: str | Path) -> None:
            Path(dst).write_bytes(b"raw")  # the other writer's identical file
            real_link(src, dst)

        with patch("receipt_index.store.os.link", side_effect=winner_first):
            ref = _original(store, b"raw")

        assert store.local_path(ref).read_bytes() == b"raw"
        assert _tmp_files(store_root) == []

    def test_rename_refuses_an_original(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        ref = _original(store, b"raw")

        with pytest.raises(OriginalImmutableError):
            store.rename(ref, "other.eml")

        assert store.exists(ref)


class TestRename:
    def test_rename_returns_the_new_reference(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        old = rendition_ref(_rendition(store, b"pdf"))

        new = store.rename(old, "2025-06-15__amazon-com__42.99.pdf")

        assert new == "local:renditions/2025/06/2025-06-15__amazon-com__42.99.pdf"
        assert not store.exists(old)
        with store.open(new) as fh:
            assert fh.read() == b"pdf"

    def test_rename_never_overwrites(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        taken = _rendition(store, b"one", vendor="Acme")
        other = rendition_ref(_rendition(store, b"two", vendor="Other"))

        new = store.rename(other, taken.rsplit("/", 1)[1])

        assert new == "local:renditions/2025/06/2025-06-15__acme__42.99_2.pdf"
        assert (store_root / taken).read_bytes() == b"one"

    def test_rename_to_its_own_name_is_a_no_op(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        ref = rendition_ref(_rendition(store))
        assert store.rename(ref, ref.rsplit("/", 1)[1]) == ref
        assert store.exists(ref)

    def test_rename_a_legacy_file_avoids_new_layout_names(
        self, store_root: Path
    ) -> None:
        store = LocalEvidenceStore(store_root)
        taken = _rendition(store, b"new", vendor="Acme")
        legacy = store_root / "2025" / "06" / "old.pdf"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy")

        new = store.rename("local:2025/06/old.pdf", taken.rsplit("/", 1)[1])

        assert new == "local:2025/06/2025-06-15__acme__42.99_2.pdf"

    def test_rename_rejects_an_unsafe_name(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        ref = rendition_ref(_rendition(store))
        with pytest.raises(ValueError, match="segment"):
            store.rename(ref, "../escape.pdf")


class TestReading:
    def test_legacy_and_new_layout_read_through_the_interface(
        self, store_root: Path
    ) -> None:
        """A pre-layout YYYY/MM receipt and a renditions/ one both read in place."""
        legacy_path = "2025/06/2025-06-15__acme-parking__42.99.pdf"
        (store_root / legacy_path).parent.mkdir(parents=True)
        (store_root / legacy_path).write_bytes(b"legacy-pdf")
        store = LocalEvidenceStore(store_root)
        new_path = _rendition(store, b"new-pdf")

        for pdf_path, data in ((legacy_path, b"legacy-pdf"), (new_path, b"new-pdf")):
            ref = rendition_ref(pdf_path)
            assert store.exists(ref)
            assert store.stat(ref) == FileStat(size=len(data), sha256=None)
            with store.open(ref) as fh:
                assert fh.read() == data
        assert (store_root / legacy_path).read_bytes() == b"legacy-pdf"

    def test_missing_file(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        assert store.exists("local:2025/06/gone.pdf") is False
        with pytest.raises(FileNotFoundError):
            store.stat("local:2025/06/gone.pdf")

    def test_a_directory_is_not_a_stored_file(self, store_root: Path) -> None:
        (store_root / "2025").mkdir()
        assert LocalEvidenceStore(store_root).exists("local:2025") is False

    def test_building_a_store_creates_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "not-yet"
        store = LocalEvidenceStore(root)
        assert store.exists("local:2025/06/x.pdf") is False
        assert not root.exists()


class TestRefsOutsideTheStore:
    @pytest.fixture()
    def store(self, store_root: Path) -> LocalEvidenceStore:
        (store_root.parent / "secret.pdf").write_bytes(b"not a receipt")
        (store_root / "link.pdf").symlink_to(store_root.parent / "secret.pdf")
        return LocalEvidenceStore(store_root)

    @pytest.mark.parametrize(
        "ref",
        ["local:../secret.pdf", "local:/etc/passwd", "local:link.pdf"],
        ids=["dot-dot", "absolute", "symlink-escape"],
    )
    def test_every_method_refuses(self, store: LocalEvidenceStore, ref: str) -> None:
        for call in (
            store.local_path,
            store.open,
            store.exists,
            store.stat,
            lambda r: store.rename(r, "x.pdf"),
        ):
            with pytest.raises(InvalidRefError) as info:
                call(ref)
            assert info.value.candidate == store.root / ref.removeprefix("local:")

    @pytest.mark.parametrize("ref", ["drive:abc", "2025/06/x.pdf", ""])
    def test_another_scheme_is_refused(
        self, store: LocalEvidenceStore, ref: str
    ) -> None:
        with pytest.raises(InvalidRefError):
            store.open(ref)

    def test_pdf_path_for_refuses_another_scheme(self) -> None:
        with pytest.raises(InvalidRefError):
            pdf_path_for("drive:abc")

    def test_a_folder_symlinked_out_of_the_store_is_refused(
        self, store_root: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (store_root / "renditions").symlink_to(outside)
        store = LocalEvidenceStore(store_root)

        with pytest.raises(InvalidRefError):
            _rendition(store)
        assert list(outside.iterdir()) == []  # no folder created outside either

    def test_a_symlink_planted_while_creating_a_folder_is_refused(
        self, store_root: Path, tmp_path: Path
    ) -> None:
        """A level created as a symlink in a race is checked before use."""
        outside = tmp_path / "outside"
        outside.mkdir()
        store = LocalEvidenceStore(store_root)
        real_mkdir = Path.mkdir

        def plant_symlink(self: Path, *args: object, **kwargs: object) -> None:
            if self.name == "renditions":
                self.symlink_to(outside)  # another process wins the race
                return
            real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

        with (
            patch("pathlib.Path.mkdir", plant_symlink),
            pytest.raises(InvalidRefError),
        ):
            _rendition(store)
        assert list(outside.iterdir()) == []

    def test_an_original_symlinked_out_of_the_store_is_not_read(
        self, store_root: Path, tmp_path: Path
    ) -> None:
        """A planted symlink at an original's target is refused, not compared."""
        store = LocalEvidenceStore(store_root)
        ref = _original(store, b"raw")
        target = store.local_path(ref)
        outside = tmp_path / "outside.eml"
        outside.write_bytes(b"raw")
        target.unlink()
        target.symlink_to(outside)

        with (
            patch("pathlib.Path.read_bytes", side_effect=AssertionError("read")),
            pytest.raises(InvalidRefError),
        ):
            _original(store, b"raw")


class TestStoreFolder:
    @pytest.mark.parametrize(
        "segment",
        [
            "",
            ".",
            "..",
            "a/b",
            "a\\b",
            "nul\x00",
            "tab\t",
            ".hidden",
            "x" * 241,
            "é" * 121,
        ],
    )
    def test_unsafe_segments_are_refused(self, segment: str) -> None:
        with pytest.raises(ValueError, match="segment"):
            StoreFolder(("2025", segment))

    def test_unsafe_name_is_refused_by_put(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        with pytest.raises(ValueError, match="segment"):
            store.put(
                b"x",
                media_type="application/pdf",
                purpose="rendition",
                folder=StoreFolder.for_rendition(date(2025, 1, 1)),
                name="../../escape.pdf",
            )
        assert list(store_root.rglob("*.pdf")) == []


class TestLongNamesAndRaces:
    def test_longest_allowed_name_is_stored(self, store_root: Path) -> None:
        """A 240-byte name and its _N twin both fit the 255-byte limit."""
        store = LocalEvidenceStore(store_root)
        folder = StoreFolder.for_rendition(date(2025, 1, 1))
        name = "x" * 236 + ".pdf"
        refs = [
            store.put(
                data,
                media_type="application/pdf",
                purpose="rendition",
                folder=folder,
                name=name,
            ).ref
            for data in (b"one", b"two")
        ]
        assert refs[1].endswith("x_2.pdf")
        assert all(store.exists(ref) for ref in refs)

    def test_rendition_lost_race_takes_the_next_name(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        real_link = os.link
        calls = []

        def other_writer_first(src: str | Path, dst: str | Path) -> None:
            calls.append(Path(dst).name)
            if len(calls) == 1:
                Path(dst).write_bytes(b"other")  # a concurrent writer's file
            real_link(src, dst)

        with patch("receipt_index.store.os.link", side_effect=other_writer_first):
            result = _rendition(store, b"mine")

        assert result.endswith("42.99_2.pdf")
        assert (store_root / result).read_bytes() == b"mine"
        assert _tmp_files(store_root) == []

    def test_rename_lost_race_takes_the_next_name(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        ref = rendition_ref(_rendition(store, b"pdf"))
        real_link = os.link
        calls = []

        def other_writer_first(src: str | Path, dst: str | Path) -> None:
            calls.append(Path(dst).name)
            if len(calls) == 1:
                Path(dst).write_bytes(b"other")
            real_link(src, dst)

        with patch("receipt_index.store.os.link", side_effect=other_writer_first):
            new = store.rename(ref, "renamed.pdf")

        assert new.endswith("/renamed_2.pdf")
        with store.open(new) as fh:
            assert fh.read() == b"pdf"
        assert not store.exists(ref)

    def test_rename_falls_back_to_a_copy_when_linking_is_refused(
        self, store_root: Path
    ) -> None:
        """With protected hard links, another owner's file can't be linked."""
        store = LocalEvidenceStore(store_root)
        old = rendition_ref(_rendition(store, b"pdf"))
        old_path = store.local_path(old)
        real_link = os.link

        def refuse_the_original(src: str | Path, dst: str | Path) -> None:
            if Path(src) == old_path:
                raise PermissionError(1, "Operation not permitted")
            real_link(src, dst)

        with patch("receipt_index.store.os.link", side_effect=refuse_the_original):
            new = store.rename(old, "renamed.pdf")

        assert new.endswith("/renamed.pdf")
        with store.open(new) as fh:
            assert fh.read() == b"pdf"
        assert not store.exists(old)
        assert _tmp_files(store_root) == []

    def test_rename_copy_fallback_takes_the_next_name(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        old = rendition_ref(_rendition(store, b"pdf"))
        old_path = store.local_path(old)
        real_link = os.link
        copies = []

        def refuse_then_collide(src: str | Path, dst: str | Path) -> None:
            if Path(src) == old_path:
                raise PermissionError(1, "Operation not permitted")
            copies.append(Path(dst).name)
            if len(copies) == 1:
                Path(dst).write_bytes(b"other")  # a concurrent writer's file
            real_link(src, dst)

        with patch("receipt_index.store.os.link", side_effect=refuse_then_collide):
            new = store.rename(old, "renamed.pdf")

        assert new.endswith("/renamed_2.pdf")
        with store.open(new) as fh:
            assert fh.read() == b"pdf"
        assert not store.exists(old)
        assert _tmp_files(store_root) == []

    def test_rename_permission_error_on_the_copy_is_raised(
        self, store_root: Path
    ) -> None:
        store = LocalEvidenceStore(store_root)
        old = rendition_ref(_rendition(store, b"pdf"))

        with (
            patch(
                "receipt_index.store.os.link",
                side_effect=PermissionError(1, "Operation not permitted"),
            ),
            pytest.raises(PermissionError),
        ):
            store.rename(old, "renamed.pdf")

        assert store.exists(old)
        assert _tmp_files(store_root) == []

    def test_rename_of_a_missing_file(self, store_root: Path) -> None:
        store = LocalEvidenceStore(store_root)
        with pytest.raises(FileNotFoundError):
            store.rename("local:renditions/2025/06/gone.pdf", "x.pdf")


class TestOpenStore:
    def test_local_backend_at_the_configured_path(self, tmp_path: Path) -> None:
        store = open_store(StoreConfig(backend="local", path=str(tmp_path)))
        assert isinstance(store, LocalEvidenceStore)
        assert store.root == tmp_path.resolve()
