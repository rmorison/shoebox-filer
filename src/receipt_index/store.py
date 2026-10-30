"""Evidence store: the storage interface and its local filesystem backend.

Callers work in bytes and opaque references, never in local paths. A
reference names one stored file: ``local:<path relative to the store root>``
for the local backend (``drive:<fileId>`` comes with the Drive backend).

Layout under the store root::

    renditions/YYYY/MM/{date}__{vendor}__{amount}.pdf
    originals/YYYY/MM/{received-date}__{source-name}__{sha256[:12]}/<name>

Receipts stored before this layout keep their ``YYYY/MM/<name>.pdf`` path.
Their reference is ``local:YYYY/MM/<name>.pdf`` and they are read where they
are; nothing moves. ``receipts.pdf_path`` holds the relative part of a local
reference, for both layouts.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, Literal, Protocol

from slugify import slugify

if TYPE_CHECKING:
    from collections.abc import Iterator
    from decimal import Decimal
    from uuid import UUID

    from receipt_index.config import StoreConfig

Purpose = Literal["original", "rendition"]

LOCAL_SCHEME = "local:"

_TOP_FOLDER: dict[str, str] = {"original": "originals", "rendition": "renditions"}
_PDF_MEDIA_TYPE = "application/pdf"
# A filesystem name is at most 255 bytes; leave room for a "_NN" suffix.
_MAX_SEGMENT_BYTES = 240
_VENDOR_SLUG_MAX = 50
_SOURCE_SLUG_MAX = 60


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StoreError(Exception):
    """Base class for evidence store errors."""


class InvalidRefError(StoreError):
    """A reference has another scheme, or names a path outside the store.

    ``candidate`` is the unresolved path the reference named, when it named a
    path at all: the store root joined with the relative part, which for an
    absolute path is that path as given.
    """

    def __init__(self, message: str, *, candidate: Path | None = None) -> None:
        super().__init__(message)
        self.candidate = candidate


class OriginalConflictError(StoreError):
    """An original already exists at the target with different bytes."""


class OriginalImmutableError(StoreError):
    """An original can't be renamed, changed or deleted."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


def _check_segment(segment: str) -> str:
    """Return ``segment`` if it is safe as one path segment, else raise."""
    if not segment or segment in {".", ".."}:
        msg = f"invalid path segment: {segment!r}"
        raise ValueError(msg)
    if segment.startswith("."):
        msg = f"path segment may not start with '.': {segment!r}"
        raise ValueError(msg)
    if len(segment.encode("utf-8")) > _MAX_SEGMENT_BYTES:
        msg = f"path segment longer than {_MAX_SEGMENT_BYTES} bytes"
        raise ValueError(msg)
    if any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in segment):
        msg = f"path segment holds a separator or control character: {segment!r}"
        raise ValueError(msg)
    return segment


@dataclass(frozen=True)
class StoreFolder:
    """A folder under a purpose's top folder, as backend-neutral segments."""

    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        for segment in self.segments:
            _check_segment(segment)

    @classmethod
    def for_rendition(cls, receipt_date: date) -> StoreFolder:
        """``YYYY/MM`` of the receipt date."""
        return cls((f"{receipt_date.year:04d}", f"{receipt_date.month:02d}"))

    @classmethod
    def for_original(
        cls, received_at: date | datetime, source_name: str, sha256: bytes
    ) -> StoreFolder:
        """``YYYY/MM/{received-date}__{source-name}__{sha256[:12]}``.

        ``sha256`` is the hash of the whole original (the ``.eml``, or the
        Drive file). The date is when the item was received, not the receipt
        date, so the folder never depends on extraction.
        """
        day = received_at.date() if isinstance(received_at, datetime) else received_at
        source = slugify(source_name, max_length=_SOURCE_SLUG_MAX) or "source"
        return cls(
            (
                f"{day.year:04d}",
                f"{day.month:02d}",
                f"{day.isoformat()}__{source}__{sha256.hex()[:12]}",
            )
        )


@dataclass(frozen=True)
class StoredFile:
    """One stored file, as ``put`` records it.

    ``id`` is the ``receipt.stored_files`` row. ``put`` writes no database
    row, so it leaves ``id`` None; the repository records the row
    (``record_rendition``) in the caller's transaction.
    """

    sha256: bytes | None
    byte_size: int | None
    media_type: str
    ref: str
    id: UUID | None = None


@dataclass(frozen=True)
class FileStat:
    """Size and the backend's own checksum (None where it keeps none)."""

    size: int
    sha256: bytes | None


class EvidenceStore(Protocol):
    """Storage for receipt evidence, in bytes and opaque references."""

    def put(
        self,
        data: bytes,
        *,
        media_type: str,
        purpose: Purpose,
        folder: StoreFolder,
        name: str,
    ) -> StoredFile: ...

    def open(self, ref: str) -> BinaryIO: ...

    def exists(self, ref: str) -> bool: ...

    def stat(self, ref: str) -> FileStat: ...

    def rename(self, ref: str, name: str) -> str: ...


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------


def _numbered(name: str) -> Iterator[str]:
    """``name``, then ``stem_2.ext``, ``stem_3.ext``, ..."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    yield name
    counter = 2
    while True:
        yield f"{stem}_{counter}{dot}{ext}"
        counter += 1


@contextmanager
def _temp_file(folder: Path, data: bytes) -> Iterator[Path]:
    """A fully written, fsynced ``.put.<random>.tmp`` file, then removed.

    The name is short and fixed, so a long target name can't push it over the
    filesystem's limit.
    """
    path = folder / f".put.{secrets.token_hex(6)}.tmp"
    try:
        with path.open("xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        yield path
    finally:
        path.unlink(missing_ok=True)


class LocalEvidenceStore:
    """The evidence store on the local filesystem, under one root.

    Building one touches nothing on disk, so read-only users (the MCP server)
    never create the tree. Every reference is resolved under the root, with
    symlinks followed, and refused if it leaves it.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    # -- references ---------------------------------------------------------

    @staticmethod
    def _ref(relative: PurePosixPath) -> str:
        return f"{LOCAL_SCHEME}{relative.as_posix()}"

    def local_path(self, ref: str) -> Path:
        """The absolute path a ``local:`` reference names, inside the root.

        Not part of :class:`EvidenceStore`: only the local backend has one.
        Raises :class:`InvalidRefError` for another scheme or a path that
        resolves outside the root.
        """
        if not ref.startswith(LOCAL_SCHEME):
            msg = f"not a local store reference: {ref!r}"
            raise InvalidRefError(msg)
        candidate = self.root / ref[len(LOCAL_SCHEME) :]
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            msg = f"reference is outside the store: {ref!r}"
            raise InvalidRefError(msg, candidate=candidate)
        return resolved

    # -- reading ------------------------------------------------------------

    def open(self, ref: str) -> BinaryIO:
        """Open a stored file for reading."""
        return self.local_path(ref).open("rb")

    def exists(self, ref: str) -> bool:
        """Whether the reference names a stored file."""
        return self.local_path(ref).is_file()

    def stat(self, ref: str) -> FileStat:
        """Size of a stored file. The local backend keeps no checksum."""
        path = self.local_path(ref)
        if not path.is_file():
            msg = f"no stored file at {ref!r}"
            raise FileNotFoundError(msg)
        return FileStat(size=path.stat().st_size, sha256=None)

    # -- writing ------------------------------------------------------------

    def _folder_path(self, relative: PurePosixPath) -> Path:
        """Create a folder under the root, one level at a time.

        Each level is checked to stay inside the root, after creating it if
        it was missing, before anything is created beneath it. A symlinked
        folder that points outside is refused without creating folders there,
        even one planted between the check and the ``mkdir``.
        """
        path = self.root
        for part in relative.parts:
            path = path / part
            if not (path.exists() or path.is_symlink()):
                path.mkdir(exist_ok=True)
            if not path.resolve().is_relative_to(self.root):
                msg = f"store folder resolves outside the store: {relative}"
                raise InvalidRefError(msg, candidate=self.root / relative)
        return path

    def _twin_folder(self, relative: PurePosixPath) -> Path | None:
        """The folder that shares rendition names with ``relative``.

        ``renditions/YYYY/MM`` and the pre-layout ``YYYY/MM`` are twins: a
        rendition name stays unique across both, so the Drive migration can
        upload legacy files into ``renditions/YYYY/MM/`` under their names.
        """
        parts = relative.parts
        if len(parts) == 3 and parts[0] == _TOP_FOLDER["rendition"]:
            return self.root.joinpath(*parts[1:])
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            return self.root.joinpath(_TOP_FOLDER["rendition"], *parts)
        return None

    @staticmethod
    def _taken(folder: Path, twin: Path | None, name: str) -> bool:
        """Whether ``name`` is taken in ``folder`` or in its twin."""
        if (folder / name).exists():
            return True
        return twin is not None and (twin / name).exists()

    def put(
        self,
        data: bytes,
        *,
        media_type: str,
        purpose: Purpose,
        folder: StoreFolder,
        name: str,
    ) -> StoredFile:
        """Store ``data`` as ``name`` in ``folder`` and return its record.

        An original is idempotent: if the target already holds the same
        bytes, nothing is written and the existing file is returned. Different
        bytes at the target raise :class:`OriginalConflictError`; an original
        is never overwritten. A rendition whose name is taken (in its folder,
        or in the legacy ``YYYY/MM`` folder) gets a numeric suffix.

        Bytes go to a temp file in the target folder, which is hard-linked
        into place. A link never overwrites, so a write is atomic and two
        concurrent writers can't clobber each other.
        """
        _check_segment(name)
        digest = hashlib.sha256(data).digest()
        relative = PurePosixPath(_TOP_FOLDER[purpose], *folder.segments)
        folder_path = self._folder_path(relative)

        def stored(final: str) -> StoredFile:
            return StoredFile(
                sha256=digest,
                byte_size=len(data),
                media_type=media_type,
                ref=self._ref(relative / final),
            )

        if purpose == "original":
            target = folder_path / name
            if not target.exists():
                with _temp_file(folder_path, data) as tmp:
                    try:
                        os.link(tmp, target)
                    except FileExistsError:
                        pass  # A concurrent writer won; compare with its file.
                    else:
                        return stored(name)
            if not target.resolve().is_relative_to(self.root):
                msg = f"stored original resolves outside the store: {relative / name}"
                raise InvalidRefError(msg, candidate=target)
            if hashlib.sha256(target.read_bytes()).digest() != digest:
                msg = (
                    "an original with different content is already stored: "
                    f"{self._ref(relative / name)}"
                )
                raise OriginalConflictError(msg)
            return stored(name)

        twin = self._twin_folder(relative)
        with _temp_file(folder_path, data) as tmp:
            for candidate in _numbered(name):
                if self._taken(folder_path, twin, candidate):
                    continue
                try:
                    os.link(tmp, folder_path / candidate)
                except FileExistsError:
                    continue
                return stored(candidate)
        msg = "unreachable: numbered names are unbounded"  # pragma: no cover
        raise AssertionError(msg)  # pragma: no cover

    def rename(self, ref: str, name: str) -> str:
        """Rename a rendition within its folder; return its new reference.

        A local reference is the file's path, so it changes with the name.
        Refuses an original with :class:`OriginalImmutableError`. A taken name
        gets a numeric suffix; a rename never overwrites.
        """
        _check_segment(name)
        path = self.local_path(ref)
        relative = PurePosixPath(path.relative_to(self.root).as_posix())
        if relative.parts and relative.parts[0] == _TOP_FOLDER["original"]:
            msg = f"originals can't be renamed: {ref!r}"
            raise OriginalImmutableError(msg)
        if not path.is_file():
            msg = f"no stored file at {ref!r}"
            raise FileNotFoundError(msg)
        twin = self._twin_folder(relative.parent)
        with ExitStack() as stack:
            source = path
            for candidate in _numbered(name):
                if candidate == path.name:
                    return self._ref(relative)
                if self._taken(path.parent, twin, candidate):
                    continue
                try:
                    os.link(source, path.parent / candidate)
                except FileExistsError:
                    continue
                except PermissionError:
                    if source != path:
                        raise
                    # With fs.protected_hardlinks, a file another user owns
                    # can't be linked: link a copy this process owns instead.
                    source = stack.enter_context(
                        _temp_file(path.parent, path.read_bytes())
                    )
                    try:
                        os.link(source, path.parent / candidate)
                    except FileExistsError:
                        continue
                path.unlink()
                return self._ref(relative.parent / candidate)
        msg = "unreachable: numbered names are unbounded"  # pragma: no cover
        raise AssertionError(msg)  # pragma: no cover


# ---------------------------------------------------------------------------
# Helpers for callers
# ---------------------------------------------------------------------------


def rendition_name(receipt_date: date, vendor: str, amount: Decimal) -> str:
    """``{YYYY-MM-DD}__{vendor-slug}__{amount}.pdf``, slug at most 50 chars."""
    slug = slugify(vendor, max_length=_VENDOR_SLUG_MAX)
    return f"{receipt_date.isoformat()}__{slug}__{amount}.pdf"


def store_rendition(
    store: EvidenceStore,
    pdf: bytes,
    *,
    receipt_date: date,
    vendor: str,
    amount: Decimal,
) -> StoredFile:
    """Store a receipt's rendered PDF under ``renditions/YYYY/MM/``."""
    return store.put(
        pdf,
        media_type=_PDF_MEDIA_TYPE,
        purpose="rendition",
        folder=StoreFolder.for_rendition(receipt_date),
        name=rendition_name(receipt_date, vendor, amount),
    )


def rendition_ref(pdf_path: str) -> str:
    """The reference for a receipt's ``pdf_path``. Validates nothing."""
    return f"{LOCAL_SCHEME}{pdf_path}"


def pdf_path_for(ref: str) -> str:
    """The ``pdf_path`` column value for a local reference."""
    if not ref.startswith(LOCAL_SCHEME):
        msg = f"only a local reference has a pdf_path: {ref!r}"
        raise InvalidRefError(msg)
    return ref[len(LOCAL_SCHEME) :]


def open_store(config: StoreConfig) -> LocalEvidenceStore:
    """The evidence store the config selects."""
    # Only the local backend exists so far; the config rejects any other.
    return LocalEvidenceStore(Path(config.path))
