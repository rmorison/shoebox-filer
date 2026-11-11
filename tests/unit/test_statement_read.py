"""Tests for reading a statement's text: receipt_index.pdf_reader.read_statement.

Every PDF here is synthetic: generated from invented text, never a real
statement.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import fields, replace
from typing import TYPE_CHECKING, Any

import pytest

from receipt_index import pdf_reader
from receipt_index.pdf_reader import (
    STATEMENT_TOKEN_CAP,
    StatementRead,
    estimate_tokens,
    page_marked_text,
    read_statement,
)
from receipt_index.pdf_validate import (
    RECEIPT_BOUNDS,
    STATEMENT_BOUNDS,
    PdfBounds,
    PdfLimits,
    output_cap,
)

if TYPE_CHECKING:
    from pathlib import Path

from tests.pdfs import (
    child_that_writes,
    expanding_font_pdf,
    fake_child,
    filter_chain_pdf,
    make_pdf,
    zero_page_pdf,
)


def _pages_pdf(*page_texts: str) -> bytes:
    """A PDF with one page per text, in order."""
    import weasyprint

    body = "<div style='page-break-after:always'></div>".join(
        f"<p>{text}</p>" for text in page_texts
    )
    pdf: bytes = weasyprint.HTML(string=f"<html><body>{body}</body></html>").write_pdf()
    return pdf


def _password_pdf() -> bytes:
    """A PDF that needs a user password to open."""
    import pikepdf

    out = io.BytesIO()
    with pikepdf.open(io.BytesIO(make_pdf("Locked statement text"))) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user="user-pw", owner="owner-pw"))
    return out.getvalue()


def _with_read(**changes: Any) -> PdfBounds:
    """STATEMENT_BOUNDS with its read limits changed."""
    return replace(STATEMENT_BOUNDS, read=replace(STATEMENT_BOUNDS.read, **changes))


# ---------------------------------------------------------------------------
# The bounds
# ---------------------------------------------------------------------------


def test_statement_bounds_set_every_limit_explicitly() -> None:
    assert STATEMENT_BOUNDS.max_read_pages == 60
    assert STATEMENT_BOUNDS.check == PdfLimits(
        cpu_seconds=20,
        wall_seconds=60.0,
        max_bytes=25 * 1024 * 1024,
        memory_bytes=512 * 1024 * 1024,
    )
    assert STATEMENT_BOUNDS.read == PdfLimits(
        cpu_seconds=90,
        wall_seconds=270.0,
        max_bytes=25 * 1024 * 1024,
        memory_bytes=1024 * 1024 * 1024,
        max_output_bytes=4 * 1024 * 1024,
    )
    for limits in (STATEMENT_BOUNDS.read, STATEMENT_BOUNDS.check):
        assert limits.cpu_seconds < limits.wall_seconds <= 3 * limits.cpu_seconds


def test_statement_bounds_dont_depend_on_the_limits_defaults() -> None:
    # Every PdfLimits in STATEMENT_BOUNDS names every field, so a later
    # change to PdfLimits' defaults can't move the statement bounds.
    import ast
    import inspect

    from receipt_index import pdf_validate

    tree = ast.parse(inspect.getsource(pdf_validate))
    (bounds,) = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "STATEMENT_BOUNDS" for t in node.targets)
    ]
    calls = [
        node
        for node in ast.walk(bounds)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "PdfLimits"
    ]
    assert len(calls) == 2
    every_field = {f.name for f in fields(PdfLimits)}
    for call in calls:
        assert {kw.arg for kw in call.keywords} == every_field


def test_the_token_cap_is_85000() -> None:
    assert STATEMENT_TOKEN_CAP == 85_000


def test_the_read_output_cap_holds_the_most_text_the_token_cap_lets_through() -> None:
    # 2.5 characters a token, every one escaped as \\uXXXX in the JSON.
    most = STATEMENT_TOKEN_CAP * 25 // 10
    worst = json.dumps(
        {"pages": 60, "page_texts": ["\u00e9" * (most // 60)] * 60, "encrypted": True}
    )
    cap = STATEMENT_BOUNDS.read.max_output_bytes
    assert cap == 4 * 1024 * 1024
    assert len(worst.encode()) < cap


def test_receipt_bounds_are_unchanged() -> None:
    assert (
        PdfBounds(
            check=PdfLimits(cpu_seconds=10, wall_seconds=30.0),
            read=PdfLimits(cpu_seconds=15, wall_seconds=45.0),
            max_read_pages=50,
        )
        == RECEIPT_BOUNDS
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_each_page_keeps_its_own_text_in_order() -> None:
    read = read_statement(
        _pages_pdf(
            "Synthetic statement page one with opening balance",
            "Synthetic statement page two with card purchases",
            "Synthetic statement page three with closing balance",
        )
    )
    assert read.reason is None
    assert read.pages == 3
    assert read.page_texts is not None
    assert [text.split()[3] for text in read.page_texts] == ["one", "two", "three"]


def test_an_empty_page_keeps_its_place() -> None:
    read = read_statement(
        _pages_pdf(
            "Synthetic first page with an opening balance",
            "",
            "Synthetic third page with a closing balance",
        )
    )
    assert read.page_texts is not None
    assert len(read.page_texts) == 3
    assert read.page_texts[1] == ""
    marked = page_marked_text(read.page_texts)
    assert "[Page 2]\n\n\n[Page 3]\nSynthetic third page" in marked


def test_a_pdf_with_no_pages_is_unreadable() -> None:
    assert read_statement(zero_page_pdf()) == StatementRead(None, reason="unreadable")
    result = b'{"pages": 0, "page_texts": [], "encrypted": false}'
    with fake_child(child_that_writes(result)):
        assert read_statement(make_pdf("No pages")) == StatementRead(
            None, reason="unreadable"
        )


def test_the_parent_checks_the_page_count_too() -> None:
    # A child that read the text anyway is still refused on its count.
    result = json.dumps(
        {"pages": 61, "page_texts": ["Synthetic page text"] * 61, "encrypted": False}
    ).encode()
    with fake_child(child_that_writes(result)):
        assert read_statement(make_pdf("Long")) == StatementRead(
            None, pages=61, reason="pages"
        )


def test_the_page_cap_comes_from_the_bounds_given() -> None:
    bounds = replace(STATEMENT_BOUNDS, max_read_pages=2)
    assert (
        read_statement(make_pdf("Synthetic page", pages=2), bounds=bounds).reason
        is None
    )
    assert read_statement(
        make_pdf("Synthetic page", pages=3), bounds=bounds
    ) == StatementRead(None, pages=3, reason="pages")


def test_a_file_exactly_at_the_size_bound_is_read() -> None:
    data = make_pdf("Synthetic statement exactly at the size bound")
    assert read_statement(data, bounds=_with_read(max_bytes=len(data))).reason is None
    assert read_statement(data, bounds=_with_read(max_bytes=len(data) - 1)) == (
        StatementRead(None, reason="size")
    )


def test_control_bidi_and_zero_width_characters_are_dropped() -> None:
    raw = (
        "\x00Syn\x07the\rtic \x85state\u202ement\u200b text\u2066\u2069"
        "\ufeff\u200e\tcolumn\nnext line"
    )
    result = json.dumps({"pages": 1, "page_texts": [raw], "encrypted": False}).encode()
    with fake_child(child_that_writes(result)):
        read = read_statement(make_pdf("Normalised"))
    assert read.page_texts == ("Synthetic statement text\tcolumn\nnext line",)


def test_a_statement_at_the_page_cap_is_read() -> None:
    read = read_statement(make_pdf("Sixty page synthetic statement", pages=60))
    assert read.reason is None
    assert read.page_texts is not None
    assert len(read.page_texts) == 60


def test_a_statement_over_the_page_cap_is_refused_with_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Decided from the page count: no page's text is read.
    import pdfplumber.page

    def never(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("a page was read")

    monkeypatch.setattr(pdfplumber.page.Page, "extract_text", never)
    assert pdf_reader._read_pages(make_pdf("Long", pages=61), text_up_to_pages=60) == (
        61,
        None,
        False,
    )
    assert read_statement(make_pdf("Long", pages=61)) == StatementRead(
        None, pages=61, reason="pages"
    )


def test_a_file_over_the_size_bound_is_refused_with_size_and_never_read() -> None:
    with fake_child("") as jobs:
        read = read_statement(make_pdf("Too big"), bounds=_with_read(max_bytes=100))
    assert read == StatementRead(None, reason="size")
    assert jobs == []


@pytest.mark.parametrize(
    "data",
    [b"not a pdf at all", b"%PDF-1.4\nbroken beyond reading"],
    ids=["not-a-pdf", "broken"],
)
def test_a_file_pdfplumber_cant_open_is_unreadable(data: bytes) -> None:
    assert read_statement(data) == StatementRead(None, reason="unreadable")


def test_a_pdf_that_needs_a_password_is_unreadable() -> None:
    assert read_statement(_password_pdf()) == StatementRead(None, reason="unreadable")


def test_a_pdf_with_only_an_owner_password_is_read() -> None:
    # Many statements carry an owner password; pdfplumber opens them, and
    # the text must come back decrypted, not empty or scrambled.
    read = read_statement(make_pdf("Owner only synthetic statement", encrypted=True))
    assert read.reason is None
    assert read.encrypted is True
    assert read.page_texts == ("Owner only synthetic statement",)


def test_an_unencrypted_pdf_says_so() -> None:
    assert read_statement(make_pdf("Plain synthetic statement")).encrypted is False


def test_a_page_set_with_no_text_is_a_scan() -> None:
    assert read_statement(make_pdf(pages=3)) == StatementRead(
        None, pages=3, reason="scan"
    )


def _read_texts(*page_texts: str) -> StatementRead:
    """read_statement of a child that found ``page_texts``."""
    result = json.dumps(
        {"pages": len(page_texts), "page_texts": list(page_texts), "encrypted": False}
    ).encode()
    with fake_child(child_that_writes(result)):
        return read_statement(make_pdf("Glyphs"))


@pytest.mark.parametrize(
    ("page_texts", "reason"),
    [
        (("(cid:12)(cid:7)(cid:3) " * 40,), "scan"),
        (("Page 1 " + "(cid:12)(cid:7) " * 40,), "scan"),
        (("(cid:4)" * 10, "(cid:4)" * 10), "scan"),
        (("Synthetic statement header text" + "(cid:12)" * 50,), "scan"),
        (("x" * 30 + "(cid:5)" * 30,), "scan"),
        (("x" * 31 + "(cid:5)" * 30,), None),
        (("Synthetic opening balance and card purchases (cid:3)",), None),
        (("Synthetic statement text", "(cid:9)"), None),
        (("Synthetic transaction line " * 220, "(cid:8)" * 4000), None),
    ],
    ids=[
        "glyphs-only",
        "a-few-words",
        "glyphs-on-every-page",
        "header-then-glyphs",
        "as-many-glyphs-as-text",
        "one-more-character",
        "a-few",
        "one-page",
        "disclosures-page",
    ],
)
def test_text_that_is_mostly_unmapped_glyphs_is_a_scan(
    page_texts: tuple[str, ...], reason: str | None
) -> None:
    # A font with no text map gives (cid:N) runs, which would reach the
    # model as noise.
    assert _read_texts(*page_texts).reason == reason


def test_unmapped_glyphs_dont_count_toward_the_minimum() -> None:
    assert _read_texts("x" * 19 + "(cid:1)" * 2).reason == "scan"
    assert _read_texts("x" * 20 + "(cid:1)" * 2).reason is None


def test_a_statement_with_just_enough_text_is_not_a_scan() -> None:
    assert read_statement(make_pdf("x" * 20)).reason is None
    assert read_statement(make_pdf("x" * 19)).reason == "scan"


# ---------------------------------------------------------------------------
# The token cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chars", "tokens"), [(0, 0), (1, 1), (5, 2), (6, 3), (250_000, 100_000)]
)
def test_the_estimate_is_two_and_a_half_characters_a_token_rounded_up(
    chars: int, tokens: int
) -> None:
    assert estimate_tokens("x" * chars) == tokens


def test_pages_are_marked_in_order() -> None:
    assert (
        page_marked_text(["first", "second"]) == "[Page 1]\nfirst\n\n[Page 2]\nsecond"
    )


def test_the_cap_counts_the_page_marked_text(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_pdf("Synthetic statement text for the cap")
    read = read_statement(data)
    assert read.page_texts is not None
    at_cap = estimate_tokens(page_marked_text(read.page_texts))

    monkeypatch.setattr(pdf_reader, "STATEMENT_TOKEN_CAP", at_cap)
    assert read_statement(data).reason is None

    monkeypatch.setattr(pdf_reader, "STATEMENT_TOKEN_CAP", at_cap - 1)
    assert read_statement(data) == StatementRead(None, pages=1, reason="text")


def test_the_cap_keeps_a_prompt_under_the_price_step() -> None:
    assert STATEMENT_TOKEN_CAP + 1_500 < 100_000


# ---------------------------------------------------------------------------
# A child that stops
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cpu_bomb() -> bytes:
    # About 700 bytes whose page content takes pdfplumber minutes of CPU.
    return filter_chain_pdf(64)


def test_a_cpu_bomb_stops_with_timeout_at_the_cpu_limit(
    cpu_bomb: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    # The wall clock is far above the CPU limit, so only the CPU limit can
    # stop it, however slow the runner; the log line shows which fired.
    bounds = _with_read(cpu_seconds=2, wall_seconds=120.0)
    with caplog.at_level("WARNING", logger="receipt_index.pdf_validate"):
        assert read_statement(cpu_bomb, bounds=bounds) == StatementRead(
            None, reason="timeout"
        )
    assert "PDF read stopped: hit the 2 s CPU limit" in caplog.text
    assert "wall-clock" not in caplog.text


def test_a_read_past_the_wall_clock_stops_with_timeout(
    cpu_bomb: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    # The CPU limit is far above the wall clock, so only the wall clock can
    # stop it; the log line shows which fired, however fast the runner.
    bounds = _with_read(cpu_seconds=60, wall_seconds=0.5)
    with caplog.at_level("WARNING", logger="receipt_index.pdf_validate"):
        assert read_statement(cpu_bomb, bounds=bounds) == StatementRead(
            None, reason="timeout"
        )
    assert "PDF read stopped: hit the 0.5 s wall-clock limit" in caplog.text


def test_a_memory_bomb_stops_with_memory() -> None:
    # 1 GiB of page content, past a 256 MB address-space limit.
    bounds = _with_read(memory_bytes=256 * 1024 * 1024)
    assert read_statement(filter_chain_pdf(1024), bounds=bounds) == StatementRead(
        None, reason="memory"
    )


_RESULT = (
    b'{"pages": 1, "page_texts": ["Synthetic text, long enough"], "encrypted": false}'
)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (-9, "timeout"),
        (-24, "timeout"),
        (-2, "timeout"),
        (-15, "timeout"),
        (-1, "timeout"),
        (3, "memory"),
        (-11, "crash"),
        (-6, "crash"),
        (-25, "text"),
        (1, "failed"),
    ],
    ids=[
        "killed",
        "cpu-limit",
        "interrupted",
        "terminated",
        "hung-up",
        "out-of-memory",
        "segfault",
        "aborted",
        "output-cap",
        "failed",
    ],
)
def test_a_stopped_child_gives_its_reason(code: int, reason: str) -> None:
    # It wrote a result before it stopped: only the exit code tells. Python
    # ignores SIGXFSZ, so the child restores it first, as the real one does.
    child = child_that_writes(_RESULT, ends_with=code)
    if code == -25:
        child = "import signal; signal.signal(signal.SIGXFSZ, signal.SIG_DFL); " + child
    with fake_child(child):
        assert read_statement(make_pdf("Stopped")) == StatementRead(
            None,
            reason=reason,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"garbage",
        b'{"pages": 1}',
        b'{"pages": 1, "page_texts": "not a list", "encrypted": false}',
        b'{"pages": 1, "page_texts": [1, 2], "encrypted": false}',
        b'{"pages": 1, "page_texts": ["Synthetic text"], "encrypted": "no"}',
        b"[1, 2]",
    ],
    ids=[
        "empty",
        "not-json",
        "missing-keys",
        "texts-not-list",
        "texts-not-str",
        "encrypted-not-bool",
        "list",
    ],
)
def test_a_child_that_writes_no_result_is_unreadable(stdout: bytes) -> None:
    with fake_child(child_that_writes(stdout)):
        assert read_statement(make_pdf("Odd child")) == StatementRead(
            None, reason="unreadable"
        )


def test_a_child_that_exits_cleanly_past_the_output_cap_reads_as_text() -> None:
    # The parent's own guard, for a child that doesn't limit itself.
    bounds = _with_read(max_bytes=1024 * 1024)
    over = output_cap(bounds.read) + 1
    child = f"import sys; sys.stdout.buffer.write(b'x' * {over}); sys.stdout.flush()"
    with fake_child(child):
        assert read_statement(make_pdf("Chatty child"), bounds=bounds) == (
            StatementRead(None, reason="text")
        )


def test_a_result_with_no_page_texts_is_over_the_page_cap() -> None:
    # The child reads no text when the PDF is over the cap it was told.
    result = b'{"pages": 3, "page_texts": null, "encrypted": false}'
    with fake_child(child_that_writes(result)):
        assert read_statement(make_pdf("No texts")) == StatementRead(
            None, pages=3, reason="pages"
        )


_PEAK_RSS = """
import resource, sys
from pathlib import Path
from receipt_index.pdf_reader import read_statement
data = Path(sys.argv[1]).read_bytes()
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
read = read_statement(data)
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(read.reason, (after - before) // 1024)
"""


def test_a_small_pdf_that_expands_to_megabytes_of_text_stays_small_in_the_parent(
    tmp_path: Path,
) -> None:
    # A 1 KB PDF whose font maps one glyph to 3,000 characters: about 9
    # million characters of text. The child's output cap stops it as
    # "text" before the parent parses it. Measured in a fresh process, as
    # the growth of its peak over its peak after imports, so nothing this
    # test run has already allocated hides it. Before the fix it grew by
    # about 240 MB on this PDF; the bound is loose because the figure
    # varies with the allocator. The child's own log line pins the cap that
    # stops it, which a parent-side check alone would not.
    pdf = tmp_path / "expanding.pdf"
    pdf.write_bytes(expanding_font_pdf(glyphs_per_page=3000, chars_per_glyph=3000))
    run = subprocess.run(
        [sys.executable, "-I", "-c", _PEAK_RSS, str(pdf)],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    reason, growth_mb = run.stdout.split()
    assert reason == "text"
    assert int(growth_mb) < 100
    cap = STATEMENT_BOUNDS.read.max_output_bytes
    assert f"PDF read stopped: hit the {cap}-byte output limit" in run.stderr


def test_a_receipt_read_still_joins_its_pages() -> None:
    read = pdf_reader.read_pdf(make_pdf("Two pages", pages=2))
    assert read is not None
    assert read.text == "Two pages\n\nTwo pages"
