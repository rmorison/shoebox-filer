"""Integration tests for PDF text extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING

import weasyprint

from receipt_index.pdf_reader import extract_text

if TYPE_CHECKING:
    import pytest


class TestPdfTextExtraction:
    """Integration tests for pdfplumber text extraction (no API key needed)."""

    def test_text_pdf_extracts_via_pdfplumber(self) -> None:
        """A real text PDF should extract text via pdfplumber without an API call."""
        html = """\
        <html><body>
        <h1>Order Confirmation</h1>
        <p>Vendor: Amazon</p>
        <p>Order Total: $42.99</p>
        <p>Date: 2025-06-15</p>
        <p>Item: Python Cookbook</p>
        </body></html>
        """
        pdf_bytes = weasyprint.HTML(string=html).write_pdf()

        result = extract_text(pdf_bytes)

        assert "Amazon" in result
        assert "42.99" in result

    def test_multipage_pdf_extracts_all_pages(self) -> None:
        """Text from all pages should be extracted."""
        html = """\
        <html><body>
        <p>Page 1: Vendor Name Corp</p>
        <div style="page-break-after: always;"></div>
        <p>Page 2: Total $199.50</p>
        </body></html>
        """
        pdf_bytes = weasyprint.HTML(string=html).write_pdf()

        result = extract_text(pdf_bytes)

        assert "Vendor Name Corp" in result
        assert "199.50" in result

    def test_image_only_pdf_gives_no_text_and_no_model_call(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An image-only PDF gives "" with no model call.

        The extraction agent then reads the PDF itself as a document block.
        """
        # weasyprint draws SVG text as paths, so pdfplumber finds no text.
        html = """\
        <html><body>
        <svg width="400" height="100" xmlns="http://www.w3.org/2000/svg">
            <text x="10" y="50" font-size="24">Receipt Total: $75.00</text>
        </svg>
        </body></html>
        """
        pdf_bytes = weasyprint.HTML(string=html).write_pdf()

        with caplog.at_level("DEBUG", logger="receipt_index.pdf_reader"):
            result = extract_text(pdf_bytes)

        assert result == ""
        assert "vision" not in caplog.text.lower()
