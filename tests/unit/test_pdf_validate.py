"""PDF validation at ingest: strict parse, repair, or unparseable (design §9)."""

from __future__ import annotations

import hashlib
import io
import resource
import subprocess
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pikepdf
import pytest

from receipt_index import pdf_validate
from receipt_index.errors import classify
from receipt_index.pdf_validate import PdfCheck, PdfLimits, _judge, check_pdf
from tests.pdfs import (
    broken_header_pdf,
    filter_chain_pdf,
    garbage_pdf,
    make_pdf,
    metadata_bomb_pdf,
    truncated_pdf,
    wrong_startxref_pdf,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    pdf_validate._cache.clear()
    yield
    pdf_validate._cache.clear()


@pytest.mark.parametrize(
    "data",
    [make_pdf("A clean receipt"), make_pdf(), make_pdf("Two", pages=2)],
    ids=["text", "blank", "two-pages"],
)
def test_a_clean_pdf_is_ok(data: bytes) -> None:
    assert check_pdf(data) == PdfCheck(status="ok")


def test_a_pdf_with_only_an_owner_password_is_ok() -> None:
    assert check_pdf(make_pdf("Owner only", encrypted=True)).status == "ok"


@pytest.mark.parametrize(
    "damaged",
    [truncated_pdf, wrong_startxref_pdf, broken_header_pdf],
    ids=["truncated", "wrong-startxref", "broken-header"],
)
def test_a_damaged_pdf_that_qpdf_rewrites_is_repaired(damaged: object) -> None:
    data = damaged()  # type: ignore[operator]
    before = hashlib.sha256(data).digest()

    result = check_pdf(data)

    assert result.status == "repaired"
    assert result.repaired is not None
    assert result.repaired != data
    # The repair parses strictly in its turn, and the input is untouched.
    assert check_pdf(result.repaired) == PdfCheck(status="ok")
    assert hashlib.sha256(data).digest() == before


def test_the_same_damaged_bytes_always_give_the_same_repair() -> None:
    data = truncated_pdf("Twice")
    first = _judge(data).repaired
    # qpdf's default file ID is seeded from the clock: let it move on.
    time.sleep(1.1)
    assert _judge(data).repaired == first


def test_a_repaired_pdf_keeps_its_pages_and_text() -> None:
    result = check_pdf(truncated_pdf("Kept text"))
    assert result.repaired is not None
    with pikepdf.open(io.BytesIO(result.repaired)) as pdf:
        assert len(pdf.pages) == 1
        assert b"Kept text" in pdf.pages[0].Contents.read_bytes()


@pytest.mark.parametrize(
    "data", [garbage_pdf(), b"", b"%PDF-1.4\n"], ids=["garbage", "empty", "header-only"]
)
def test_bytes_that_cannot_be_repaired_are_unparseable(data: bytes) -> None:
    assert check_pdf(data) == PdfCheck(status="unparseable")


def test_a_pdf_that_needs_a_user_password_is_unparseable() -> None:
    with patch(
        "receipt_index.pdf_validate.pikepdf.open",
        side_effect=pikepdf.PasswordError("password needed"),
    ):
        assert _judge(make_pdf("Locked")) == PdfCheck(status="unparseable")


def test_a_repair_that_still_fails_strict_mode_is_unparseable() -> None:
    real_open = pikepdf.open
    calls: list[bool] = []

    def open_(*args: object, **kwargs: object) -> pikepdf.Pdf:
        strict = kwargs.get("attempt_recovery") is False
        calls.append(strict)
        if strict:
            raise pikepdf.PdfError("still damaged")
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    with patch("receipt_index.pdf_validate.pikepdf.open", side_effect=open_):
        assert _judge(truncated_pdf()) == PdfCheck(status="unparseable")
    # The strict open, the recovering open, and the strict check of the repair.
    assert calls == [True, False, True]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("qpdf"), RecursionError(), OverflowError(), MemoryError()],
)
def test_any_error_reading_a_pdf_makes_it_unparseable(error: Exception) -> None:
    # In the child, a MemoryError is its address-space limit: the file's doing.
    with patch("receipt_index.pdf_validate.pikepdf.open", side_effect=error):
        assert _judge(make_pdf("Hostile")) == PdfCheck(status="unparseable")


def test_a_memory_error_reaching_an_item_is_transient() -> None:
    assert classify(MemoryError()) == "transient"


# ---------------------------------------------------------------------------
# Hostile PDFs: qpdf runs in a bounded child, never in this process
# ---------------------------------------------------------------------------

_TIGHT = PdfLimits(memory_bytes=256 * 1024 * 1024, cpu_seconds=1, wall_seconds=20)


@pytest.fixture(scope="module")
def memory_bomb() -> bytes:
    return metadata_bomb_pdf(512)  # ~1.5 KB that inflates to 512 MiB on save


@pytest.fixture(scope="module")
def cpu_bomb() -> bytes:
    return filter_chain_pdf(256)  # ~1 KB that takes seconds of CPU to save


def _peak_rss_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


@pytest.mark.parametrize("bomb", ["memory_bomb", "cpu_bomb"])
def test_a_hostile_pdf_is_unparseable_within_the_bound_and_this_process_stays_small(
    bomb: str, request: pytest.FixtureRequest
) -> None:
    data = request.getfixturevalue(bomb)
    before = _peak_rss_mb()
    start = time.monotonic()

    result = check_pdf(data, limits=_TIGHT)

    assert result == PdfCheck(status="unparseable")
    assert time.monotonic() - start < _TIGHT.wall_seconds
    assert _peak_rss_mb() - before < 100


def test_a_check_that_runs_past_the_wall_clock_is_unparseable(cpu_bomb: bytes) -> None:
    limits = PdfLimits(cpu_seconds=60, wall_seconds=0.5)
    start = time.monotonic()
    assert check_pdf(cpu_bomb, limits=limits) == PdfCheck(status="unparseable")
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("code", [-9, -24, 1])
def test_a_killed_or_failed_child_is_unparseable(code: int) -> None:
    # It wrote a result before it was killed: only the exit code tells.
    done = subprocess.CompletedProcess(
        args=[], returncode=code, stdout=b"ok\n", stderr=b"Killed"
    )
    with patch("receipt_index.pdf_validate.subprocess.run", return_value=done):
        assert check_pdf(make_pdf("Killed")) == PdfCheck(status="unparseable")


def test_a_pdf_over_the_size_cap_is_unparseable_without_a_check() -> None:
    with patch("receipt_index.pdf_validate.subprocess.run") as run:
        result = check_pdf(make_pdf("Too big"), limits=PdfLimits(max_bytes=100))
    assert result == PdfCheck(status="unparseable")
    run.assert_not_called()


def test_the_same_bytes_are_checked_once() -> None:
    real = subprocess.run
    calls: list[Any] = []

    def run(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real(*args, **kwargs)

    data = truncated_pdf("Once")
    with patch("receipt_index.pdf_validate.subprocess.run", side_effect=run):
        first, second = check_pdf(data), check_pdf(data)
    assert first == second and first.status == "repaired"
    assert len(calls) == 1


def test_a_timed_out_check_is_not_remembered() -> None:
    data = make_pdf("Slow host")
    timeout = subprocess.TimeoutExpired(cmd="pdf check", timeout=1)
    with patch("receipt_index.pdf_validate.subprocess.run", side_effect=timeout):
        assert check_pdf(data).status == "unparseable"
    assert check_pdf(data).status == "ok"
