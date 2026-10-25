"""Integration tests for PDF rendering."""

from __future__ import annotations

import pytest


def _chromium_installed() -> bool:
    """Check if Playwright Chromium is available."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(
    not _chromium_installed(),
    reason="Playwright Chromium not installed",
)
class TestHtmlToPdfPlaywright:
    """Integration test with real Playwright headless Chromium."""

    def test_produces_valid_pdf(self) -> None:
        from receipt_index.renderer import _html_to_pdf_playwright

        result = _html_to_pdf_playwright("<html><body><p>Hello</p></body></html>")
        assert isinstance(result, bytes)
        assert result[:5] == b"%PDF-"

    def test_complex_html_table(self) -> None:
        from receipt_index.renderer import _html_to_pdf_playwright

        html = """\
        <html><body>
        <table>
          <tr><td>Item 1</td><td style="text-align:right">$42.99</td></tr>
          <tr><td>Item 2</td><td style="text-align:right">$15.00</td></tr>
          <tr><td><strong>Total</strong></td><td><strong>$57.99</strong></td></tr>
        </table>
        </body></html>
        """
        result = _html_to_pdf_playwright(html)
        assert isinstance(result, bytes)
        assert result[:5] == b"%PDF-"
        assert len(result) > 1000  # non-trivial PDF
