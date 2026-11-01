"""PDF validation at ingest: strict parse, repair, or unparseable (design §9)."""

from __future__ import annotations

import hashlib
import io
import json
import resource
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pikepdf
import pytest

from receipt_index import pdf_validate
from receipt_index.errors import classify
from receipt_index.pdf_validate import (
    RECEIPT_BOUNDS,
    PdfCheck,
    PdfLimits,
    _judge,
    check_pdf,
    run_bounded,
)
from tests.pdfs import (
    broken_header_pdf,
    child_that_writes,
    fake_child,
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
        "pikepdf.open",
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

    with patch("pikepdf.open", side_effect=open_):
        assert _judge(truncated_pdf()) == PdfCheck(status="unparseable")
    # The strict open, the recovering open, and the strict check of the repair.
    assert calls == [True, False, True]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("qpdf"), RecursionError(), OverflowError(), MemoryError()],
)
def test_any_error_reading_a_pdf_makes_it_unparseable(error: Exception) -> None:
    # In the child, a MemoryError is its address-space limit: the file's doing.
    with patch("pikepdf.open", side_effect=error):
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


def test_under_the_default_limits_a_memory_bomb_is_unparseable() -> None:
    # 1 GiB inflated: past the default 512 MB address-space limit.
    data = metadata_bomb_pdf(1024)
    before = _peak_rss_mb()
    start = time.monotonic()
    assert check_pdf(data) == PdfCheck(status="unparseable")
    assert time.monotonic() - start < RECEIPT_BOUNDS.check.wall_seconds
    assert _peak_rss_mb() - before < 100


def test_a_check_that_runs_past_the_wall_clock_is_unparseable(cpu_bomb: bytes) -> None:
    limits = PdfLimits(cpu_seconds=60, wall_seconds=0.5)
    start = time.monotonic()
    assert check_pdf(cpu_bomb, limits=limits) == PdfCheck(status="unparseable")
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("code", [-9, -24, 1])
def test_a_killed_or_failed_child_is_unparseable(code: int) -> None:
    # It wrote a result before it was killed: only the exit code tells.
    with fake_child(child_that_writes(b"ok\n", ends_with=code)):
        assert check_pdf(make_pdf("Killed")) == PdfCheck(status="unparseable")


@pytest.mark.parametrize("stdout", [b"", b"bogus\n"], ids=["empty", "unknown"])
def test_a_child_that_writes_no_status_is_unparseable(stdout: bytes) -> None:
    data = make_pdf("Odd child")
    with fake_child(child_that_writes(stdout)):
        assert check_pdf(data) == PdfCheck(status="unparseable")
    # Not remembered: the next, real check judges the bytes afresh.
    assert check_pdf(data) == PdfCheck(status="ok")


def test_a_pdf_over_the_size_cap_is_unparseable_without_a_check() -> None:
    with fake_child("") as jobs:
        limits = replace(RECEIPT_BOUNDS.check, max_bytes=100)
        result = check_pdf(make_pdf("Too big"), limits=limits)
    assert result == PdfCheck(status="unparseable")
    assert jobs == []


def test_the_same_bytes_are_checked_once() -> None:
    real = pdf_validate._command
    calls: list[str] = []

    def command(job: str, limits: PdfLimits, args: tuple[str, ...]) -> list[str]:
        calls.append(job)
        return real(job, limits, args)

    data = truncated_pdf("Once")
    with patch("receipt_index.pdf_validate._command", command):
        first, second = check_pdf(data), check_pdf(data)
    assert first == second and first.status == "repaired"
    assert calls == ["check"]


def test_a_timed_out_check_is_not_remembered() -> None:
    data = make_pdf("Slow host")
    limits = replace(RECEIPT_BOUNDS.check, wall_seconds=0.5)
    with fake_child("import time; time.sleep(10)"):
        assert check_pdf(data, limits=limits).status == "unparseable"
    assert check_pdf(data).status == "ok"


# ---------------------------------------------------------------------------
# The child's output is bounded in the parent
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]


def _in_fresh_process(script: str) -> dict[str, Any]:
    """Run ``script`` in a new interpreter; it prints one JSON line.

    A fresh process, so an earlier high-water mark can't hide this one's.
    """
    proc = subprocess.run(  # our own interpreter and a test script
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=_REPO,
        capture_output=True,
        timeout=120,
        check=True,
    )
    return json.loads(proc.stdout.decode().strip().splitlines()[-1])  # type: ignore[no-any-return]


_MEASURE = """
    import json, resource, time
    from receipt_index import pdf_validate
    from receipt_index.pdf_reader import read_pdf
    from receipt_index.pdf_validate import PdfLimits, check_pdf, run_bounded
    from tests.pdfs import warning_flood_pdf

    TIGHT = PdfLimits(cpu_seconds=2, wall_seconds=20)

    def peak():
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024

    {setup}
    before, start = peak(), time.monotonic()
    result = {call}
    if isinstance(result, bytes):  # never echo a flood back
        result = f"{{len(result)}} bytes"
    print(json.dumps({{
        "result": repr(result),
        "seconds": time.monotonic() - start,
        "growth_mb": peak() - before,
    }}))
"""


def test_a_pdf_that_floods_warnings_leaves_the_parent_small() -> None:
    # 40 KB that makes pdfminer log hundreds of megabytes; once buffered whole.
    call = "read_pdf(warning_flood_pdf(), limits=TIGHT)"
    out = _in_fresh_process(_MEASURE.format(setup="", call=call))
    # With nothing logged, a fast host may finish the read inside the limit.
    assert out["result"] in ("None", "PdfRead(pages=5, text='', encrypted=False)")
    assert out["seconds"] < 20
    assert out["growth_mb"] < 100


_FLOOD_CHILD = (
    "import sys; chunk = b'x' * (1 << 20)\n"
    "for _ in range(300): sys.{stream}.buffer.write(chunk)"
)


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_a_child_that_floods_its_output_leaves_the_parent_small(stream: str) -> None:
    # A child that ignores its own limits: the parent still keeps a bound.
    setup = (
        "assert hasattr(pdf_validate, '_command')\n    "
        "pdf_validate._command = lambda job, limits, args: "
        f"[__import__('sys').executable, '-c', {_FLOOD_CHILD.format(stream=stream)!r}]"
    )
    call = "run_bounded('read', b'%PDF', PdfLimits(cpu_seconds=10, wall_seconds=30))"
    out = _in_fresh_process(_MEASURE.format(setup=setup, call=call))
    if stream == "stdout":
        assert out["result"] == "None"  # 300 MB is past the output cap
    assert out["seconds"] < 30
    assert out["growth_mb"] < 100


def test_a_child_that_writes_past_the_output_cap_is_stopped_and_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The real child entry point, with a job that writes 4 MB past a 1 MB cap.
    def command(job: str, limits: PdfLimits, args: tuple[str, ...]) -> list[str]:
        cap = pdf_validate.output_cap(limits)
        script = (
            "import sys; from receipt_index import pdf_validate as v; "
            "v._check_job = lambda data: b'x' * (4 << 20); "
            f"sys.argv = ['child', 'check', '{512 << 20}', '10', '{cap}']; "
            "v._child_main()"
        )
        return [sys.executable, "-c", script]

    limits = replace(RECEIPT_BOUNDS.check, max_bytes=1000)
    with (
        patch("receipt_index.pdf_validate._command", command),
        caplog.at_level("WARNING", logger="receipt_index.pdf_validate"),
    ):
        assert run_bounded("check", b"%PDF", limits) is None
    cap = pdf_validate.output_cap(limits)
    assert f"PDF check stopped: hit the {cap}-byte output limit" in caplog.text


@pytest.mark.parametrize(
    ("code", "named"),
    [(-signal.SIGRTMIN, "SIGRTMIN"), (-40, "signal 40"), (7, "exit code 7")],
)
def test_any_ending_is_named_without_failing(code: int, named: str) -> None:
    assert named in pdf_validate._stopped_by(code, RECEIPT_BOUNDS.read)


def test_the_child_limits_core_dumps_and_output_size() -> None:
    script = """
        import resource, sys
        from receipt_index import pdf_validate as v
        v._check_job = lambda data: b" ".join(
            str(resource.getrlimit(r)[0]).encode()
            for r in (resource.RLIMIT_CORE, resource.RLIMIT_FSIZE)
        )
        sys.argv = ["child", "check", str(512 << 20), "10", str(51 << 20)]
        v._child_main()
    """
    proc = subprocess.run(  # our own interpreter and a test script
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=_REPO,
        input=b"",
        capture_output=True,
        timeout=60,
        check=True,
    )
    core, fsize = (int(n) for n in proc.stdout.split())
    assert core == 0
    assert 0 < fsize <= pdf_validate.output_cap(RECEIPT_BOUNDS.check)


def test_the_child_logs_nothing() -> None:
    # pdfminer's warnings would otherwise reach stderr through Python's
    # last-resort handler.
    script = """
        import logging, sys, warnings
        from receipt_index import pdf_validate as v
        def job(data):
            logging.getLogger("pdfminer").warning("x" * 1000)
            warnings.warn("y" * 1000)
            return b"ok\\n"
        v._check_job = job
        sys.argv = ["child", "check", str(512 << 20), "10", str(51 << 20)]
        v._child_main()
    """
    proc = subprocess.run(  # our own interpreter and a test script
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=_REPO,
        input=b"",
        capture_output=True,
        timeout=60,
        check=True,
    )
    assert proc.stdout == b"ok\n"
    assert proc.stderr == b""
