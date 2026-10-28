"""Tests for receipt_index.pdf_reader."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from receipt_index import pdf_reader
from receipt_index.pdf_reader import (
    _extract_with_pdfplumber,
    _is_sufficient_text,
    extract_text,
)


def _make_text_pdf(text: str) -> bytes:
    """Generate a minimal text PDF using weasyprint for test fixtures."""
    import weasyprint

    html = f"<html><body><p>{text}</p></body></html>"
    return weasyprint.HTML(string=html).write_pdf()


class TestExtractWithPdfplumber:
    """Tests for pdfplumber text extraction."""

    def test_extracts_text_from_text_pdf(self) -> None:
        pdf_bytes = _make_text_pdf("Amazon Order Total: $42.99")
        result = _extract_with_pdfplumber(pdf_bytes)
        assert "Amazon" in result
        assert "42.99" in result

    def test_returns_empty_for_corrupt_pdf(self) -> None:
        result = _extract_with_pdfplumber(b"not a pdf at all")
        assert result == ""

    def test_returns_empty_for_empty_bytes(self) -> None:
        result = _extract_with_pdfplumber(b"")
        assert result == ""


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

    @patch("receipt_index.pdf_reader._extract_with_pdfplumber")
    def test_uses_pdfplumber_when_sufficient(self, mock_plumber: MagicMock) -> None:
        mock_plumber.return_value = "Amazon Order Total: $42.99 on 2025-01-15"

        result = extract_text(b"%PDF-fake")

        assert "Amazon" in result

    @patch("receipt_index.pdf_reader._extract_with_pdfplumber")
    def test_returns_empty_when_text_insufficient(
        self, mock_plumber: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A scanned PDF yields too little text. The caller then sends the
        # PDF itself to the extraction agent; there is no text-only model call.
        mock_plumber.return_value = "Total"

        with caplog.at_level("DEBUG", logger="receipt_index.pdf_reader"):
            result = extract_text(b"%PDF-fake")

        assert result == ""
        assert "vision" not in caplog.text.lower()

    def test_takes_no_vision_agent(self) -> None:
        with pytest.raises(TypeError):
            extract_text(b"%PDF-fake", vision_agent=MagicMock())  # type: ignore[call-arg]

    def test_has_no_vision_agent_factory(self) -> None:
        assert not hasattr(pdf_reader, "create_vision_agent")
        assert not hasattr(pdf_reader, "_extract_with_vision")
