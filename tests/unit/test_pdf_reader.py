"""Tests for receipt_index.pdf_reader."""

from __future__ import annotations

import resource
import time
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from receipt_index import pdf_reader
from receipt_index.pdf_reader import (
    PdfRead,
    _is_sufficient_text,
    _read_unbounded,
    extract_text,
    read_pdf,
)
from receipt_index.pdf_validate import RECEIPT_BOUNDS, PdfLimits
from tests.pdfs import child_that_writes, fake_child, filter_chain_pdf, make_pdf


def _make_text_pdf(text: str) -> bytes:
    """Generate a minimal text PDF using weasyprint for test fixtures."""
    import weasyprint

    html = f"<html><body><p>{text}</p></body></html>"
    return weasyprint.HTML(string=html).write_pdf()


class TestReadUnbounded:
    """pdfplumber itself, as the child runs it."""

    def test_extracts_text_from_text_pdf(self) -> None:
        read = _read_unbounded(_make_text_pdf("Amazon Order Total: $42.99"))
        assert read is not None
        assert read.text is not None
        assert "Amazon" in read.text
        assert "42.99" in read.text

    def test_returns_none_for_corrupt_pdf(self) -> None:
        assert _read_unbounded(b"not a pdf at all") is None

    def test_returns_none_for_empty_bytes(self) -> None:
        assert _read_unbounded(b"") is None

    def test_a_long_pdf_is_not_read_through_past_the_page_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pdfplumber.page

        calls: list[int] = []
        real = pdfplumber.page.Page.extract_text

        def counting(page: pdfplumber.page.Page, *args: object, **kw: object) -> str:
            calls.append(page.page_number)
            return real(page, *args, **kw)

        monkeypatch.setattr(pdfplumber.page.Page, "extract_text", counting)
        read = _read_unbounded(make_pdf("Statement", pages=11), text_up_to_pages=10)
        assert read == PdfRead(pages=11, text=None, encrypted=False)
        assert calls == []

    def test_a_page_read_error_is_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args: object, **_kwargs: object) -> str:
            raise ValueError("bad page")

        monkeypatch.setattr("pdfplumber.page.Page.extract_text", boom)
        assert (
            _read_unbounded(make_pdf("Acme Parking (U.S.), LLC. Total 12.34")) is None
        )


class TestIsSufficientText:
    """Tests for the text quality threshold."""

    def test_sufficient_text(self) -> None:
        assert _is_sufficient_text("Amazon Order Total: $42.99") is True

    def test_insufficient_text(self) -> None:
        assert _is_sufficient_text("   ") is False

    def test_empty_string(self) -> None:
        assert _is_sufficient_text("") is False

    def test_whitespace_only(self) -> None:
        assert _is_sufficient_text("\n\n  \t  ") is False

    def test_boundary_at_threshold(self) -> None:
        assert _is_sufficient_text("a" * 20) is True
        assert _is_sufficient_text("a" * 19) is False

    def test_whitespace_not_counted(self) -> None:
        # 10 chars + whitespace should be insufficient
        assert _is_sufficient_text("a " * 10) is False


class TestExtractText:
    """Tests for the top-level extract_text function."""

    @patch("receipt_index.pdf_reader.read_pdf")
    def test_uses_pdfplumber_when_sufficient(self, mock_read: MagicMock) -> None:
        mock_read.return_value = PdfRead(
            1, "Amazon Order Total: $42.99 on 2025-01-15", False
        )

        result = extract_text(b"%PDF-fake")

        assert "Amazon" in result

    @patch("receipt_index.pdf_reader.read_pdf")
    def test_returns_empty_when_text_insufficient(
        self, mock_read: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A scanned PDF yields too little text. The caller then sends the
        # PDF itself to the extraction agent; there is no text-only model call.
        mock_read.return_value = PdfRead(1, "Total", False)

        with caplog.at_level("DEBUG", logger="receipt_index.pdf_reader"):
            result = extract_text(b"%PDF-fake")

        assert result == ""
        assert "vision" not in caplog.text.lower()

    @patch("receipt_index.pdf_reader.read_pdf", return_value=None)
    def test_returns_empty_when_unreadable(self, _read: MagicMock) -> None:
        assert extract_text(b"%PDF-fake") == ""

    def test_reads_a_real_pdf(self) -> None:
        assert extract_text(make_pdf("Acme Parking (U.S.), LLC. Total 12.34")) == (
            "Acme Parking (U.S.), LLC. Total 12.34"
        )

    def test_takes_no_vision_agent(self) -> None:
        with pytest.raises(TypeError):
            extract_text(b"%PDF-fake", vision_agent=MagicMock())  # type: ignore[call-arg]

    def test_has_no_vision_agent_factory(self) -> None:
        assert not hasattr(pdf_reader, "create_vision_agent")
        assert not hasattr(pdf_reader, "_extract_with_vision")
        # No model client in this module, so extract_text can't call one.
        assert not hasattr(pdf_reader, "Agent")


# ---------------------------------------------------------------------------
# The bounded read: pdfplumber runs in a limited child process
# ---------------------------------------------------------------------------

_TIGHT = PdfLimits(cpu_seconds=2, wall_seconds=20)


@pytest.mark.parametrize(
    "data",
    [
        make_pdf("Acme Parking (U.S.), LLC. Total 12.34"),
        make_pdf(),
        make_pdf("Two pages", pages=2),
        make_pdf("Eleven pages", pages=11),
        make_pdf("Owner only", encrypted=True),
        b"not a pdf at all",
    ],
    ids=["text", "blank", "two-pages", "eleven-pages", "encrypted", "not-a-pdf"],
)
@pytest.mark.parametrize("limit", [None, 10], ids=["all-pages", "up-to-10"])
def test_the_child_reads_exactly_what_pdfplumber_reads(
    data: bytes, limit: int | None
) -> None:
    assert read_pdf(data, text_up_to_pages=limit) == _read_unbounded(
        data, text_up_to_pages=limit
    )


def test_a_weasyprint_pdf_reads_the_same_in_the_child() -> None:
    data = _make_text_pdf("Amazon Order Total: $42.99")
    assert read_pdf(data) == _read_unbounded(data)


def test_a_multi_page_pdf_joins_its_pages_with_a_blank_line() -> None:
    assert read_pdf(make_pdf("Two pages", pages=2)) == PdfRead(
        pages=2, text="Two pages\n\nTwo pages", encrypted=False
    )


def _long_table_pdf(sections: int) -> bytes:
    """An ordinary statement-like PDF: one dense table page per section."""
    import weasyprint

    rows = "".join(
        f"<tr><td>2026-01-{i % 28 + 1:02d}</td><td>Parking session {i}</td>"
        f"<td>{i * 1.37:.2f}</td><td>Acme Parking (U.S.), LLC</td></tr>"
        for i in range(40)
    )
    section = f"<table>{rows}</table><div style='page-break-after:always'></div>"
    return weasyprint.HTML(
        string=f"<html><body>{section * sections}</body></html>"
    ).write_pdf()


def test_a_pdf_at_the_page_cap_is_read() -> None:
    read = read_pdf(make_pdf("Fifty", pages=50))
    assert read is not None
    assert read.pages == 50
    assert read.text is not None


def test_a_pdf_over_the_page_cap_gives_no_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Decided from the page count. The patch reaches only this process, so
    # the _read_unbounded call is what shows no page's text is read.
    import pdfplumber.page

    def never(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("a page was read")

    monkeypatch.setattr(pdfplumber.page.Page, "extract_text", never)
    assert _read_unbounded(make_pdf("Long", pages=51), text_up_to_pages=50) == (
        PdfRead(pages=51, text=None, encrypted=False)
    )
    with caplog.at_level("INFO", logger="receipt_index.pdf_reader"):
        assert read_pdf(make_pdf("Long", pages=51)) is None
        assert extract_text(make_pdf("Long", pages=51)) == ""
    assert "over the 50-page cap" in caplog.text


@pytest.mark.parametrize(("up_to", "sent"), [(None, "50"), (10, "10"), (80, "50")])
def test_the_child_is_told_the_page_cap(up_to: int | None, sent: str) -> None:
    with patch("receipt_index.pdf_reader.run_bounded", return_value=b"null") as run:
        read_pdf(b"%PDF-1.4", text_up_to_pages=up_to)
    assert run.call_args.args[-1] == sent


def test_receipt_bounds_are_the_operators_limits() -> None:
    assert RECEIPT_BOUNDS.read.cpu_seconds == 15
    assert RECEIPT_BOUNDS.check.cpu_seconds == 10
    assert RECEIPT_BOUNDS.max_read_pages == 50
    for limits in (RECEIPT_BOUNDS.read, RECEIPT_BOUNDS.check):
        assert limits.cpu_seconds < limits.wall_seconds <= 3 * limits.cpu_seconds


def test_a_long_ordinary_pdf_reads_in_flat_memory() -> None:
    # pdfplumber keeps every page's layout objects until the file closes
    # unless each page is closed: kept, these 20 pages need over 144 MB.
    data = _long_table_pdf(20)
    limits = replace(RECEIPT_BOUNDS.read, memory_bytes=128 * 1024 * 1024)
    read = read_pdf(data, limits=limits)
    assert read is not None
    assert read.pages == 20
    assert read.text is not None
    assert read.text.count("Parking session 39 53.43") == 20


def _peak_rss_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


@pytest.fixture(scope="module")
def cpu_bomb() -> bytes:
    # About 700 bytes whose page content takes pdfplumber minutes of CPU.
    return filter_chain_pdf(64)


def test_a_cpu_bomb_gives_no_text_within_the_bound(
    cpu_bomb: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    before = _peak_rss_mb()
    start = time.monotonic()

    with caplog.at_level("WARNING", logger="receipt_index.pdf_validate"):
        assert read_pdf(cpu_bomb, limits=_TIGHT) is None

    assert "PDF read stopped: hit the 2 s CPU limit" in caplog.text

    assert time.monotonic() - start < _TIGHT.wall_seconds
    assert _peak_rss_mb() - before < 100


def test_under_the_default_limits_a_memory_bomb_gives_no_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 1 GiB of page content: past the default 512 MB address-space limit.
    data = filter_chain_pdf(1024)
    before = _peak_rss_mb()
    start = time.monotonic()

    with caplog.at_level("WARNING", logger="receipt_index.pdf_validate"):
        assert extract_text(data) == ""

    assert "PDF read stopped: hit the 512 MB memory limit" in caplog.text

    assert time.monotonic() - start < RECEIPT_BOUNDS.read.wall_seconds
    assert _peak_rss_mb() - before < 100


def test_a_read_that_runs_past_the_wall_clock_gives_no_text(
    cpu_bomb: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    limits = PdfLimits(cpu_seconds=60, wall_seconds=0.5)
    start = time.monotonic()
    with caplog.at_level("WARNING", logger="receipt_index.pdf_validate"):
        assert read_pdf(cpu_bomb, limits=limits) is None
    assert "PDF read stopped: hit the 0 s wall-clock limit" not in caplog.text
    assert "PDF read stopped: hit the 0.5 s wall-clock limit" in caplog.text
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("code", [-9, -24, 1])
def test_a_killed_or_failed_child_gives_no_text(code: int) -> None:
    # It wrote a result before it was killed: only the exit code tells.
    result = b'{"pages": 1, "text": "Total 12.34 paid in full", "encrypted": false}'
    with fake_child(child_that_writes(result, ends_with=code)):
        assert read_pdf(make_pdf("Killed")) is None


@pytest.mark.parametrize(
    "stdout",
    [b"", b"garbage", b'{"pages": 1}', b"[1, 2]"],
    ids=["empty", "not-json", "missing-keys", "wrong-shape"],
)
def test_a_child_that_writes_no_result_gives_no_text(stdout: bytes) -> None:
    with fake_child(child_that_writes(stdout)):
        assert read_pdf(make_pdf("Odd child")) is None


def test_a_pdf_over_the_size_cap_is_not_read() -> None:
    with fake_child("") as jobs:
        limits = replace(RECEIPT_BOUNDS.read, max_bytes=100)
        assert read_pdf(make_pdf("Too big"), limits=limits) is None
    assert jobs == []
