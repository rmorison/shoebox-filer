"""Tests for receipt_index.pdf_reader."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pydantic_ai import BinaryContent

from receipt_index.pdf_reader import (
    _extract_with_pdfplumber,
    _extract_with_vision,
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


class TestExtractWithVision:
    """Tests for Claude vision fallback."""

    def test_calls_agent_with_binary_content(self) -> None:
        mock_result = MagicMock()
        mock_result.output = "Amazon Order $42.99"
        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = mock_result

        result = _extract_with_vision(b"%PDF-fake", agent=mock_agent)

        assert result == "Amazon Order $42.99"
        call_args = mock_agent.run_sync.call_args[0][0]
        assert isinstance(call_args[1], BinaryContent)
        assert call_args[1].media_type == "application/pdf"

    def test_returns_empty_on_agent_failure(self) -> None:
        mock_agent = MagicMock()
        mock_agent.run_sync.side_effect = RuntimeError("API error")

        result = _extract_with_vision(b"%PDF-fake", agent=mock_agent)
        assert result == ""


class TestExtractText:
    """Tests for the top-level extract_text function."""

    @patch("receipt_index.pdf_reader._extract_with_pdfplumber")
    def test_uses_pdfplumber_when_sufficient(self, mock_plumber: MagicMock) -> None:
        mock_plumber.return_value = "Amazon Order Total: $42.99 on 2025-01-15"

        result = extract_text(b"%PDF-fake")

        assert "Amazon" in result

    @patch("receipt_index.pdf_reader._extract_with_vision")
    @patch("receipt_index.pdf_reader._extract_with_pdfplumber")
    def test_falls_back_to_vision_when_insufficient(
        self, mock_plumber: MagicMock, mock_vision: MagicMock
    ) -> None:
        mock_plumber.return_value = ""
        mock_vision.return_value = "Scanned receipt content"

        result = extract_text(b"%PDF-fake")

        assert result == "Scanned receipt content"
        mock_vision.assert_called_once()

    @patch("receipt_index.pdf_reader._extract_with_vision")
    @patch("receipt_index.pdf_reader._extract_with_pdfplumber")
    def test_returns_empty_when_both_fail(
        self, mock_plumber: MagicMock, mock_vision: MagicMock
    ) -> None:
        mock_plumber.return_value = ""
        mock_vision.return_value = ""

        result = extract_text(b"%PDF-fake")

        assert result == ""

    @patch("receipt_index.pdf_reader._extract_with_pdfplumber")
    def test_does_not_call_vision_when_pdfplumber_succeeds(
        self, mock_plumber: MagicMock
    ) -> None:
        mock_plumber.return_value = "Sufficient text content here for extraction"

        with patch("receipt_index.pdf_reader._extract_with_vision") as mock_vision:
            extract_text(b"%PDF-fake")
            mock_vision.assert_not_called()

    @patch("receipt_index.pdf_reader._extract_with_vision")
    @patch("receipt_index.pdf_reader._extract_with_pdfplumber")
    def test_passes_vision_agent_through(
        self, mock_plumber: MagicMock, mock_vision: MagicMock
    ) -> None:
        mock_plumber.return_value = ""
        mock_vision.return_value = "vision result"
        mock_agent = MagicMock()

        extract_text(b"%PDF-fake", vision_agent=mock_agent)

        mock_vision.assert_called_once_with(b"%PDF-fake", agent=mock_agent)
