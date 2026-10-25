"""Tests for receipt_index.renderer."""

from __future__ import annotations

import base64
import logging
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from receipt_index.models import Attachment, RawReceipt
from receipt_index.renderer import (
    RemoteResourceBlockedError,
    _block_remote_requests,
    _embed_inline_images,
    _find_pdf_attachment,
    _html_to_pdf_bytes,
    _html_to_pdf_playwright,
    _html_to_pdf_weasyprint,
    _inline_only_url_fetcher,
    _is_inline_url,
    _render_drive_file,
    _render_text_to_pdf,
    render_pdf,
)

if TYPE_CHECKING:
    from playwright.sync_api import Route

# A 1x1 transparent GIF, small enough to embed inline in test fixtures.
INLINE_GIF_DATA_URI = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)
REMOTE_IMAGE_URL = "https://example.invalid/tracker.png"


def _chromium_available() -> bool:
    """Report whether Playwright Chromium can be launched in this environment."""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", ".playwright")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:
        return False


CHROMIUM_AVAILABLE = _chromium_available()


class TestRenderPdf:
    """Tests for the top-level render_pdf function."""

    def test_pdf_attachment_passthrough(self) -> None:
        pdf_data = b"%PDF-1.4 test content"
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="Receipt",
            sender="shop@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            attachments=[
                Attachment(
                    filename="receipt.pdf",
                    content_type="application/pdf",
                    data=pdf_data,
                )
            ],
        )
        result = render_pdf(raw)
        assert result == pdf_data

    @patch("receipt_index.renderer._html_to_pdf_bytes")
    def test_html_body_rendered(self, mock_pdf: pytest.fixture) -> None:
        mock_pdf.return_value = b"pdf-from-html"
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="HTML Receipt",
            sender="shop@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            html_body="<p>Your receipt</p>",
        )
        result = render_pdf(raw)
        assert result == b"pdf-from-html"
        mock_pdf.assert_called_once()

    @patch("receipt_index.renderer._html_to_pdf_weasyprint")
    def test_text_body_rendered(self, mock_pdf: pytest.fixture) -> None:
        mock_pdf.return_value = b"pdf-from-text"
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="Text Receipt",
            sender="shop@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text_body="Your total: $42.99",
        )
        result = render_pdf(raw)
        assert result == b"pdf-from-text"

    @patch("receipt_index.renderer._html_to_pdf_weasyprint")
    def test_no_body_fallback(self, mock_pdf: pytest.fixture) -> None:
        mock_pdf.return_value = b"pdf-fallback"
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="Empty",
            sender="x@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
        )
        result = render_pdf(raw)
        assert result == b"pdf-fallback"

    def test_pdf_attachment_preferred_over_html(self) -> None:
        pdf_data = b"%PDF-1.4 original"
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="Receipt",
            sender="shop@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            html_body="<p>Receipt HTML</p>",
            attachments=[
                Attachment(
                    filename="receipt.pdf",
                    content_type="application/pdf",
                    data=pdf_data,
                )
            ],
        )
        result = render_pdf(raw)
        assert result == pdf_data


class TestFindPdfAttachment:
    """Tests for _find_pdf_attachment."""

    def test_finds_pdf(self) -> None:
        pdf_data = b"%PDF data"
        attachments = [
            Attachment(
                filename="doc.pdf", content_type="application/pdf", data=pdf_data
            )
        ]
        assert _find_pdf_attachment(attachments) == pdf_data

    def test_returns_none_when_no_pdf(self) -> None:
        attachments = [
            Attachment(filename="image.png", content_type="image/png", data=b"png")
        ]
        assert _find_pdf_attachment(attachments) is None

    def test_returns_none_for_empty_list(self) -> None:
        assert _find_pdf_attachment([]) is None

    def test_first_pdf_wins(self) -> None:
        attachments = [
            Attachment(
                filename="first.pdf",
                content_type="application/pdf",
                data=b"first",
            ),
            Attachment(
                filename="second.pdf",
                content_type="application/pdf",
                data=b"second",
            ),
        ]
        assert _find_pdf_attachment(attachments) == b"first"

    def test_ignores_non_pdf(self) -> None:
        attachments = [
            Attachment(filename="img.jpg", content_type="image/jpeg", data=b"jpg"),
            Attachment(filename="doc.pdf", content_type="application/pdf", data=b"pdf"),
        ]
        assert _find_pdf_attachment(attachments) == b"pdf"


class TestEmbedInlineImages:
    """Tests for _embed_inline_images."""

    def test_replaces_cid_reference(self) -> None:
        img_data = b"\x89PNG"
        attachments = [
            Attachment(filename="logo", content_type="image/png", data=img_data)
        ]
        html = '<img src="cid:logo">'
        result = _embed_inline_images(html, attachments)

        expected_b64 = base64.b64encode(img_data).decode("ascii")
        assert f"data:image/png;base64,{expected_b64}" in result
        assert "cid:" not in result

    def test_multiple_images(self) -> None:
        attachments = [
            Attachment(filename="img1", content_type="image/png", data=b"png1"),
            Attachment(filename="img2", content_type="image/jpeg", data=b"jpg2"),
        ]
        html_content = '<img src="cid:img1"><img src="cid:img2">'
        result = _embed_inline_images(html_content, attachments)

        assert "data:image/png;base64," in result
        assert "data:image/jpeg;base64," in result

    def test_no_cid_passthrough(self) -> None:
        html_content = '<img src="https://example.com/img.png">'
        result = _embed_inline_images(html_content, [])
        assert result == html_content

    def test_missing_attachment_keeps_cid(self) -> None:
        html_content = '<img src="cid:missing">'
        attachments = [
            Attachment(filename="other", content_type="image/png", data=b"png")
        ]
        result = _embed_inline_images(html_content, attachments)
        assert "cid:missing" in result

    def test_non_image_attachments_ignored(self) -> None:
        html_content = '<img src="cid:doc">'
        attachments = [
            Attachment(filename="doc", content_type="application/pdf", data=b"pdf")
        ]
        result = _embed_inline_images(html_content, attachments)
        # PDF attachments should not be in the image map
        assert "cid:doc" in result


class TestRenderTextToPdf:
    """Tests for _render_text_to_pdf."""

    @patch("receipt_index.renderer._html_to_pdf_weasyprint")
    def test_template_includes_subject_and_sender(
        self, mock_pdf: pytest.fixture
    ) -> None:
        mock_pdf.return_value = b"pdf-bytes"
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="My Receipt",
            sender="shop@example.com",
            date=datetime(2025, 3, 15, tzinfo=UTC),
            text_body="Total: $100",
        )
        _render_text_to_pdf(raw)

        call_args = mock_pdf.call_args[0][0]
        assert "My Receipt" in call_args
        assert "shop@example.com" in call_args
        assert "2025-03-15" in call_args
        assert "Total: $100" in call_args

    @patch("receipt_index.renderer._html_to_pdf_weasyprint")
    def test_html_escapes_body(self, mock_pdf: pytest.fixture) -> None:
        mock_pdf.return_value = b"pdf-bytes"
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="Test",
            sender="x@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text_body="Price: <script>alert('xss')</script>",
        )
        _render_text_to_pdf(raw)

        call_args = mock_pdf.call_args[0][0]
        assert "<script>" not in call_args
        assert "&lt;script&gt;" in call_args


class TestHtmlToPdfBytes:
    """Tests for the _html_to_pdf_bytes dispatcher."""

    @pytest.fixture(autouse=True)
    def _suppress_weasyprint_warnings(self, caplog: pytest.LogCaptureFixture) -> None:
        """Suppress noisy weasyprint CSS warnings in test output."""
        caplog.set_level(logging.ERROR, logger="weasyprint")

    def test_produces_valid_pdf(self) -> None:
        result = _html_to_pdf_bytes("<html><body><p>Hello</p></body></html>")
        assert isinstance(result, bytes)
        assert result[:5] == b"%PDF-"

    def test_unicode_content(self) -> None:
        result = _html_to_pdf_bytes(
            "<html><body><p>Price: \u20ac42.99</p></body></html>"
        )
        assert isinstance(result, bytes)
        assert result[:5] == b"%PDF-"

    @patch(
        "receipt_index.renderer._html_to_pdf_playwright",
        side_effect=RuntimeError("no chromium"),
    )
    @patch("receipt_index.renderer._html_to_pdf_weasyprint", return_value=b"%PDF-ws")
    def test_falls_back_to_weasyprint(
        self, mock_ws: MagicMock, _mock_pw: MagicMock
    ) -> None:
        result = _html_to_pdf_bytes("<p>test</p>")
        assert result == b"%PDF-ws"
        mock_ws.assert_called_once_with("<p>test</p>")

    @patch("receipt_index.renderer._html_to_pdf_playwright", return_value=b"%PDF-pw")
    def test_prefers_playwright(self, mock_pw: MagicMock) -> None:
        result = _html_to_pdf_bytes("<p>test</p>")
        assert result == b"%PDF-pw"
        mock_pw.assert_called_once_with("<p>test</p>")


class TestHtmlToPdfWeasyprint:
    """Integration test with real weasyprint."""

    @pytest.fixture(autouse=True)
    def _suppress_weasyprint_warnings(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.ERROR, logger="weasyprint")

    def test_produces_valid_pdf(self) -> None:
        result = _html_to_pdf_weasyprint("<html><body><p>Hello</p></body></html>")
        assert isinstance(result, bytes)
        assert result[:5] == b"%PDF-"


class TestRenderDriveFile:
    """Tests for _render_drive_file."""

    def test_pdf_passthrough(self) -> None:
        pdf_data = b"%PDF-1.4 drive document"
        raw = RawReceipt(
            source_id="drive-1",
            source_name="test-source",
            source_type="gdrive",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            file_content=pdf_data,
            file_content_type="application/pdf",
        )
        result = _render_drive_file(raw)
        assert result == pdf_data

    @patch(
        "receipt_index.image_converter.image_to_pdf", return_value=b"%PDF-from-image"
    )
    def test_image_jpeg_converted(self, mock_convert: MagicMock) -> None:
        image_data = b"\xff\xd8\xff\xe0fake-jpeg"
        raw = RawReceipt(
            source_id="drive-2",
            source_name="test-source",
            source_type="gdrive",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            file_content=image_data,
            file_content_type="image/jpeg",
        )
        result = _render_drive_file(raw)
        assert result == b"%PDF-from-image"
        mock_convert.assert_called_once_with(image_data)

    @patch("receipt_index.image_converter.image_to_pdf", return_value=b"%PDF-from-png")
    def test_image_png_converted(self, mock_convert: MagicMock) -> None:
        image_data = b"\x89PNGfake-png"
        raw = RawReceipt(
            source_id="drive-3",
            source_name="test-source",
            source_type="gdrive",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            file_content=image_data,
            file_content_type="image/png",
        )
        result = _render_drive_file(raw)
        assert result == b"%PDF-from-png"
        mock_convert.assert_called_once_with(image_data)

    def test_raises_without_file_content(self) -> None:
        raw = RawReceipt(
            source_id="drive-4",
            source_name="test-source",
            source_type="gdrive",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            file_content=None,
            file_content_type="application/pdf",
        )
        with pytest.raises(ValueError, match="no file_content"):
            _render_drive_file(raw)

    def test_raises_for_unsupported_type(self) -> None:
        raw = RawReceipt(
            source_id="drive-5",
            source_name="test-source",
            source_type="gdrive",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            file_content=b"data",
            file_content_type="text/plain",
        )
        with pytest.raises(ValueError, match="Unsupported file content type"):
            _render_drive_file(raw)


class TestRenderPdfDriveRouting:
    """Tests for render_pdf routing to Drive path."""

    def test_gdrive_pdf_routes_to_drive_path(self) -> None:
        pdf_data = b"%PDF-1.4 drive pdf"
        raw = RawReceipt(
            source_id="drive-1",
            source_name="test-source",
            source_type="gdrive",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            file_content=pdf_data,
            file_content_type="application/pdf",
        )
        result = render_pdf(raw)
        assert result == pdf_data

    @patch("receipt_index.image_converter.image_to_pdf", return_value=b"%PDF-converted")
    def test_gdrive_image_routes_to_drive_path(self, _mock_convert: MagicMock) -> None:
        raw = RawReceipt(
            source_id="drive-2",
            source_name="test-source",
            source_type="gdrive",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            file_content=b"\xff\xd8\xff\xe0jpeg",
            file_content_type="image/jpeg",
        )
        result = render_pdf(raw)
        assert result == b"%PDF-converted"


class TestIsInlineUrl:
    """Tests for the shared inline/remote URL classifier."""

    @pytest.mark.parametrize(
        "url",
        [
            INLINE_GIF_DATA_URI,
            "data:text/plain,hello",
            "DATA:text/plain,hello",
            "about:blank",
            "blob:null/2b8f0e6a",
        ],
    )
    def test_inline_urls_allowed(self, url: str) -> None:
        assert _is_inline_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.invalid/x.png",
            "http://example.invalid/x.png",
            "file:///etc/passwd",
            "ftp://example.invalid/x.png",
            "//example.invalid/x.png",
            "/local/x.png",
            "x.png",
            "",
        ],
    )
    def test_remote_urls_refused(self, url: str) -> None:
        assert _is_inline_url(url) is False


class TestBlockRemoteRequests:
    """Tests for the Playwright route handler."""

    @staticmethod
    def _route(url: str) -> MagicMock:
        route = MagicMock()
        route.request.url = url
        return route

    def test_remote_request_aborted(self) -> None:
        route = self._route(REMOTE_IMAGE_URL)
        _block_remote_requests(route)

        route.abort.assert_called_once_with()
        route.continue_.assert_not_called()

    def test_file_url_aborted(self) -> None:
        route = self._route("file:///etc/passwd")
        _block_remote_requests(route)

        route.abort.assert_called_once_with()

    def test_inline_request_allowed(self) -> None:
        route = self._route(INLINE_GIF_DATA_URI)
        _block_remote_requests(route)

        route.continue_.assert_called_once_with()
        route.abort.assert_not_called()

    def test_about_blank_allowed(self) -> None:
        route = self._route("about:blank")
        _block_remote_requests(route)

        route.continue_.assert_called_once_with()
        route.abort.assert_not_called()


class TestInlineOnlyUrlFetcher:
    """Tests for the weasyprint URL fetcher."""

    @pytest.mark.parametrize(
        "url",
        [
            REMOTE_IMAGE_URL,
            "http://example.invalid/x.png",
            "file:///etc/passwd",
            "/local/x.png",
        ],
    )
    def test_remote_url_refused(self, url: str) -> None:
        with pytest.raises(RemoteResourceBlockedError, match="Remote resource blocked"):
            _inline_only_url_fetcher(url)

    def test_data_uri_served(self) -> None:
        payload = b"inline-bytes"
        b64 = base64.b64encode(payload).decode("ascii")
        response = _inline_only_url_fetcher(
            f"data:application/octet-stream;base64,{b64}"
        )

        assert response.read() == payload


class TestWeasyprintEgressBlocking:
    """Rendering with the weasyprint engine must not fetch remote resources."""

    @pytest.fixture(autouse=True)
    def _suppress_weasyprint_warnings(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.CRITICAL, logger="weasyprint")

    def test_remote_image_blocked_render_succeeds(self) -> None:
        html_content = (
            f'<html><body><p>Total: $42.99</p><img src="{REMOTE_IMAGE_URL}">'
            "</body></html>"
        )
        result = _html_to_pdf_weasyprint(html_content)

        assert result[:5] == b"%PDF-"
        assert len(result) > 500

    def test_remote_stylesheet_blocked_render_succeeds(self) -> None:
        html_content = (
            '<html><head><link rel="stylesheet" '
            'href="https://example.invalid/style.css"></head>'
            "<body><p>Total: $42.99</p></body></html>"
        )
        result = _html_to_pdf_weasyprint(html_content)

        assert result[:5] == b"%PDF-"

    def test_fetcher_is_wired_into_render(self) -> None:
        requested: list[str] = []

        def spy(url: str) -> object:
            requested.append(url)
            # Call the module-level import, not renderer._inline_only_url_fetcher —
            # the latter is patched to this spy and would recurse.
            return _inline_only_url_fetcher(url)

        html_content = f'<html><body><img src="{REMOTE_IMAGE_URL}"></body></html>'
        with patch("receipt_index.renderer._inline_only_url_fetcher", spy):
            result = _html_to_pdf_weasyprint(html_content)

        assert result[:5] == b"%PDF-"
        assert REMOTE_IMAGE_URL in requested

    def test_text_body_render_produces_pdf(self) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="Text Receipt",
            sender="shop@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text_body="Your total: $42.99",
        )
        result = _render_text_to_pdf(raw)

        assert result[:5] == b"%PDF-"
        assert len(result) > 500


@pytest.mark.skipif(
    not CHROMIUM_AVAILABLE,
    reason="Playwright Chromium not installed",
)
class TestPlaywrightEgressBlocking:
    """Rendering with real Chromium must not fetch remote resources."""

    def test_inline_only_html_renders(self) -> None:
        html_content = (
            f'<html><body><p>Total: $42.99</p><img src="{INLINE_GIF_DATA_URI}">'
            "</body></html>"
        )
        result = _html_to_pdf_playwright(html_content)

        assert result[:5] == b"%PDF-"
        assert len(result) > 500

    def test_remote_image_blocked_render_completes(self) -> None:
        seen: list[str] = []

        def spy(route: Route) -> None:
            seen.append(route.request.url)
            # Call the module-level import, not renderer._block_remote_requests —
            # the latter is patched to this spy and would recurse.
            _block_remote_requests(route)

        html_content = (
            f'<html><body><p>Total: $42.99</p><img src="{REMOTE_IMAGE_URL}">'
            "</body></html>"
        )
        started = time.monotonic()
        with patch("receipt_index.renderer._block_remote_requests", spy):
            result = _html_to_pdf_playwright(html_content)
        elapsed = time.monotonic() - started

        assert result[:5] == b"%PDF-"
        assert REMOTE_IMAGE_URL in seen, "request never reached the route handler"
        # Blocked != timeout: aborts are immediate, so the render stays fast.
        assert elapsed < 30
