"""Integration tests for PDF text extraction."""

from __future__ import annotations

import os

import pytest
import weasyprint

from receipt_index.pdf_reader import create_vision_agent, extract_text


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


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY required for vision integration test",
)
class TestPdfVisionFallback:
    """Integration tests requiring Anthropic API for vision fallback."""

    def test_vision_extracts_from_image_pdf(self) -> None:
        """An image-only PDF should fall back to vision and extract text."""
        # Create a PDF where text is rendered as an image (SVG path),
        # making it unextractable by pdfplumber. We use an SVG embedded
        # in HTML to simulate this.
        html = """\
        <html><body>
        <svg width="400" height="100" xmlns="http://www.w3.org/2000/svg">
            <text x="10" y="50" font-size="24">Receipt Total: $75.00</text>
        </svg>
        </body></html>
        """
        pdf_bytes = weasyprint.HTML(string=html).write_pdf()

        agent = create_vision_agent(api_key=os.environ["ANTHROPIC_API_KEY"])
        result = extract_text(pdf_bytes, vision_agent=agent)

        # Vision should extract the text from the rendered image
        assert "75.00" in result or "75" in result
