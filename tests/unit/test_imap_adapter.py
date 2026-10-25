"""Tests for receipt_index.adapters.imap."""

from __future__ import annotations

import hashlib
import imaplib
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from receipt_index.adapters.imap import ImapAdapter

if TYPE_CHECKING:
    from receipt_index.config import ImapSourceConfig


def _make_simple_email(
    *,
    subject: str = "Test Subject",
    sender: str = "sender@example.com",
    date: str = "Mon, 15 Jun 2025 10:30:00 +0000",
    message_id: str | None = "<test-1@example.com>",
    body: str = "Hello, World!",
    html: bool = False,
) -> bytes:
    """Build a simple email message as bytes."""
    subtype = "html" if html else "plain"
    msg = MIMEText(body, subtype)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Date"] = date
    if message_id:
        msg["Message-ID"] = message_id
    return msg.as_bytes()


def _make_multipart_email(
    *,
    subject: str = "Test Subject",
    sender: str = "sender@example.com",
    date: str = "Mon, 15 Jun 2025 10:30:00 +0000",
    message_id: str = "<test-multi@example.com>",
    text_body: str | None = "Plain text body",
    html_body: str | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
    inline_images: list[tuple[str, str, bytes]] | None = None,
) -> bytes:
    """Build a multipart email with optional attachments and inline images."""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Date"] = date
    msg["Message-ID"] = message_id

    if text_body or html_body:
        alt = MIMEMultipart("alternative")
        if text_body:
            alt.attach(MIMEText(text_body, "plain"))
        if html_body:
            alt.attach(MIMEText(html_body, "html"))
        msg.attach(alt)

    if attachments:
        for filename, content_type, data in attachments:
            _maintype, subtype = content_type.split("/", 1)
            att = MIMEApplication(data, subtype)
            att.add_header("Content-Disposition", "attachment", filename=filename)
            att["Content-Type"] = content_type
            msg.attach(att)

    if inline_images:
        for content_id, content_type, data in inline_images:
            _maintype, subtype = content_type.split("/", 1)
            img = MIMEImage(data, subtype)
            img.add_header("Content-ID", f"<{content_id}>")
            img.add_header("Content-Disposition", "inline")
            msg.attach(img)

    return msg.as_bytes()


class TestGetMessageId:
    """Tests for _get_message_id."""

    def test_extracts_message_id_header(self) -> None:
        from email import message_from_bytes

        raw = _make_simple_email(message_id="<unique-123@mail.com>")
        msg = message_from_bytes(raw)
        result = ImapAdapter._get_message_id(msg)
        assert result == "<unique-123@mail.com>"

    def test_strips_whitespace_from_message_id(self) -> None:
        from email import message_from_bytes

        raw = _make_simple_email(message_id="  <spaced@mail.com>  ")
        msg = message_from_bytes(raw)
        result = ImapAdapter._get_message_id(msg)
        assert result == "<spaced@mail.com>"

    def test_fallback_hash_when_no_message_id(self) -> None:
        from email import message_from_bytes

        raw = _make_simple_email(message_id=None)
        msg = message_from_bytes(raw)
        result = ImapAdapter._get_message_id(msg)

        # Should be a SHA-256 hex digest
        assert len(result) == 64
        expected_key = f"{msg['Subject']}|{msg['Date']}|{msg['From']}"
        expected = hashlib.sha256(expected_key.encode()).hexdigest()
        assert result == expected


class TestDecodeHeaderValue:
    """Tests for _decode_header_value."""

    def test_simple_ascii(self) -> None:
        assert ImapAdapter._decode_header_value("Hello World") == "Hello World"

    def test_rfc2047_encoded(self) -> None:
        encoded = "=?utf-8?B?SMOpbGzDqA==?="
        result = ImapAdapter._decode_header_value(encoded)
        assert "H" in result  # Contains the decoded character

    def test_none_returns_empty(self) -> None:
        assert ImapAdapter._decode_header_value(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert ImapAdapter._decode_header_value("") == ""


class TestExtractBodyAndAttachments:
    """Tests for _extract_body_and_attachments."""

    def test_plain_text_only(self) -> None:
        from email import message_from_bytes

        raw = _make_simple_email(body="Just text", html=False)
        msg = message_from_bytes(raw)
        html_body, text_body, attachments = ImapAdapter._extract_body_and_attachments(
            msg
        )
        assert text_body == "Just text"
        assert html_body is None
        assert attachments == []

    def test_html_only(self) -> None:
        from email import message_from_bytes

        raw = _make_simple_email(body="<h1>Hello</h1>", html=True)
        msg = message_from_bytes(raw)
        html_body, text_body, attachments = ImapAdapter._extract_body_and_attachments(
            msg
        )
        assert html_body == "<h1>Hello</h1>"
        assert text_body is None
        assert attachments == []

    def test_multipart_with_text_and_html(self) -> None:
        from email import message_from_bytes

        raw = _make_multipart_email(
            text_body="Plain version",
            html_body="<p>HTML version</p>",
        )
        msg = message_from_bytes(raw)
        html_body, text_body, attachments = ImapAdapter._extract_body_and_attachments(
            msg
        )
        assert text_body == "Plain version"
        assert html_body == "<p>HTML version</p>"
        assert attachments == []

    def test_pdf_attachment(self) -> None:
        from email import message_from_bytes

        pdf_data = b"%PDF-1.4 fake"
        raw = _make_multipart_email(
            attachments=[("receipt.pdf", "application/pdf", pdf_data)]
        )
        msg = message_from_bytes(raw)
        _html, _text, attachments = ImapAdapter._extract_body_and_attachments(msg)
        assert len(attachments) == 1
        assert attachments[0].filename == "receipt.pdf"
        assert attachments[0].content_type == "application/pdf"
        assert attachments[0].data == pdf_data

    def test_inline_image(self) -> None:
        from email import message_from_bytes

        img_data = b"\x89PNG\r\n\x1a\n"  # PNG header bytes
        raw = _make_multipart_email(
            html_body='<img src="cid:logo123">',
            inline_images=[("logo123", "image/png", img_data)],
        )
        msg = message_from_bytes(raw)
        _html, _text, attachments = ImapAdapter._extract_body_and_attachments(msg)
        # Inline image should be captured as an attachment
        image_atts = [a for a in attachments if a.content_type.startswith("image/")]
        assert len(image_atts) >= 1

    def test_multiple_attachments(self) -> None:
        from email import message_from_bytes

        raw = _make_multipart_email(
            attachments=[
                ("file1.pdf", "application/pdf", b"pdf1"),
                ("file2.csv", "text/csv", b"csv data"),
            ]
        )
        msg = message_from_bytes(raw)
        _html, _text, attachments = ImapAdapter._extract_body_and_attachments(msg)
        assert len(attachments) == 2
        filenames = {a.filename for a in attachments}
        assert "file1.pdf" in filenames
        assert "file2.csv" in filenames


class TestFetchUnprocessed:
    """Tests for the fetch_unprocessed flow."""

    def _mock_imap_connection(self, messages: dict[bytes, bytes]) -> MagicMock:
        """Create a mock IMAP4_SSL with predefined messages."""
        conn = MagicMock()
        conn.select.return_value = ("OK", [b"1"])

        msg_ids = b" ".join(messages.keys()) if messages else b""
        conn.search.return_value = ("OK", [msg_ids])

        def fake_fetch(msg_id: str, _fmt: str) -> tuple[str, list[object]]:
            # msg_id comes in as str (decoded in _fetch_message)
            data = messages.get(msg_id.encode())
            if data is None:
                return ("OK", [None])
            return ("OK", [(b"1 (RFC822 {100})", data)])

        conn.fetch.side_effect = fake_fetch
        return conn

    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_yields_unprocessed_messages(
        self, mock_ssl: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        email1 = _make_simple_email(
            message_id="<msg-1@example.com>", subject="Receipt 1"
        )
        email2 = _make_simple_email(
            message_id="<msg-2@example.com>", subject="Receipt 2"
        )

        conn = self._mock_imap_connection({b"1": email1, b"2": email2})
        mock_ssl.return_value = conn

        adapter = ImapAdapter(imap_config)
        results = list(adapter.fetch_unprocessed(set()))

        assert len(results) == 2
        assert results[0].source_id == "<msg-1@example.com>"
        assert results[1].source_id == "<msg-2@example.com>"

    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_skips_processed_messages(
        self, mock_ssl: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        email1 = _make_simple_email(
            message_id="<msg-1@example.com>", subject="Receipt 1"
        )
        email2 = _make_simple_email(
            message_id="<msg-2@example.com>", subject="Receipt 2"
        )

        conn = self._mock_imap_connection({b"1": email1, b"2": email2})
        mock_ssl.return_value = conn

        adapter = ImapAdapter(imap_config)
        results = list(adapter.fetch_unprocessed({"<msg-1@example.com>"}))

        assert len(results) == 1
        assert results[0].source_id == "<msg-2@example.com>"

    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_empty_folder(
        self, mock_ssl: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        conn = self._mock_imap_connection({})
        mock_ssl.return_value = conn

        adapter = ImapAdapter(imap_config)
        results = list(adapter.fetch_unprocessed(set()))

        assert results == []

    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_logout_called_on_success(
        self, mock_ssl: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        conn = self._mock_imap_connection({})
        mock_ssl.return_value = conn

        adapter = ImapAdapter(imap_config)
        list(adapter.fetch_unprocessed(set()))

        conn.logout.assert_called_once()

    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_logout_called_on_error(
        self, mock_ssl: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        conn = MagicMock()
        conn.select.side_effect = Exception("Connection lost")
        mock_ssl.return_value = conn

        adapter = ImapAdapter(imap_config)
        with pytest.raises(Exception, match="Connection lost"):
            list(adapter.fetch_unprocessed(set()))

        conn.logout.assert_called_once()

    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_connection_uses_config(
        self, mock_ssl: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        conn = self._mock_imap_connection({})
        mock_ssl.return_value = conn

        adapter = ImapAdapter(imap_config)
        list(adapter.fetch_unprocessed(set()))

        mock_ssl.assert_called_once_with("imap.example.com", 993)
        conn.login.assert_called_once_with("test@example.com", "secret")
        conn.select.assert_called_once_with("INBOX", readonly=True)

    @patch("receipt_index.adapters.imap.imaplib.IMAP4")
    def test_plain_imap_when_ssl_disabled(
        self, mock_imap: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        plain_config = imap_config.model_copy(update={"use_ssl": False, "port": 143})
        conn = self._mock_imap_connection({})
        mock_imap.return_value = conn

        adapter = ImapAdapter(plain_config)
        list(adapter.fetch_unprocessed(set()))

        mock_imap.assert_called_once_with("imap.example.com", 143)
        conn.login.assert_called_once_with("test@example.com", "secret")

    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_parses_email_fields(
        self, mock_ssl: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        email_bytes = _make_simple_email(
            subject="Your Order",
            sender="shop@store.com",
            date="Mon, 15 Jun 2025 10:30:00 +0000",
            message_id="<order-1@store.com>",
            body="Total: $50.00",
        )

        conn = self._mock_imap_connection({b"1": email_bytes})
        mock_ssl.return_value = conn

        adapter = ImapAdapter(imap_config)
        results = list(adapter.fetch_unprocessed(set()))

        assert len(results) == 1
        receipt = results[0]
        assert receipt.subject == "Your Order"
        assert receipt.sender == "shop@store.com"
        assert receipt.text_body == "Total: $50.00"
        assert receipt.source_id == "<order-1@store.com>"


class TestConnectRetry:
    """Tests for IMAP connection retry logic."""

    @patch("receipt_index.adapters.imap.time.sleep")
    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_retries_on_connection_failure(
        self, mock_ssl: MagicMock, mock_sleep: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        conn = MagicMock()
        conn.select.return_value = ("OK", [b"1"])
        conn.search.return_value = ("OK", [b""])

        # Fail twice, then succeed
        mock_ssl.side_effect = [OSError("refused"), OSError("refused"), conn]

        adapter = ImapAdapter(imap_config)
        list(adapter.fetch_unprocessed(set()))

        assert mock_ssl.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch("receipt_index.adapters.imap.time.sleep")
    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_raises_after_max_retries(
        self, mock_ssl: MagicMock, mock_sleep: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        mock_ssl.side_effect = OSError("connection refused")

        adapter = ImapAdapter(imap_config)
        with pytest.raises(ConnectionError, match="Failed to connect to IMAP after 3"):
            list(adapter.fetch_unprocessed(set()))

        assert mock_ssl.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("receipt_index.adapters.imap.imaplib.IMAP4_SSL")
    def test_imap_error_not_retried(
        self, mock_ssl: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        """IMAP protocol/auth errors should propagate immediately, not retry."""
        mock_ssl.side_effect = imaplib.IMAP4.error("auth fail")

        adapter = ImapAdapter(imap_config)
        with pytest.raises(imaplib.IMAP4.error, match="auth fail"):
            list(adapter.fetch_unprocessed(set()))

        assert mock_ssl.call_count == 1
