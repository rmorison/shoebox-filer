"""IMAP source adapter."""

from __future__ import annotations

import hashlib
import imaplib
import logging
import time
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, cast

from receipt_index.models import Attachment, RawReceipt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from email.message import Message

    from receipt_index.config import ImapSourceConfig

logger = logging.getLogger(__name__)

_MAX_CONNECT_RETRIES = 3


class ImapAdapter:
    """Fetch unprocessed receipts from an IMAP mailbox."""

    def __init__(self, config: ImapSourceConfig) -> None:
        self.config = config
        self.name = config.name

    def fetch_unprocessed(self, processed_ids: set[str]) -> Iterator[RawReceipt]:
        """Connect to IMAP, fetch messages, yield those not yet processed."""
        conn: imaplib.IMAP4 | None = None
        try:
            conn = self._connect()
            msg_ids = self._fetch_message_ids(conn)

            for msg_id in msg_ids:
                raw_email = self._fetch_message(conn, msg_id)
                if raw_email is None:
                    continue

                msg = message_from_bytes(raw_email)
                source_id = self._get_message_id(msg)

                if source_id in processed_ids:
                    logger.debug("Skipping already-processed message %s", source_id)
                    continue

                try:
                    receipt = self._parse_message(msg, source_id)
                    yield receipt
                except Exception:
                    logger.warning(
                        "Failed to parse message %s", source_id, exc_info=True
                    )
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    logger.debug("Error during IMAP logout", exc_info=True)

    def _connect(self) -> imaplib.IMAP4:
        """Establish an IMAP connection and authenticate.

        Retries up to 3 times with exponential backoff on connection errors.
        """
        last_err: Exception | None = None
        for attempt in range(_MAX_CONNECT_RETRIES):
            try:
                if self.config.use_ssl:
                    conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                        self.config.host, self.config.port
                    )
                else:
                    conn = imaplib.IMAP4(self.config.host, self.config.port)
                conn.login(self.config.username, self.config.password)
                return conn
            except imaplib.IMAP4.error:
                raise  # auth/protocol errors are not transient
            except OSError as exc:
                last_err = exc
                if attempt < _MAX_CONNECT_RETRIES - 1:
                    delay = 2**attempt
                    logger.warning(
                        "IMAP connection attempt %d failed, retrying in %ds: %s",
                        attempt + 1,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
        raise ConnectionError(
            f"Failed to connect to IMAP after {_MAX_CONNECT_RETRIES} attempts"
        ) from last_err

    def _fetch_message_ids(self, conn: imaplib.IMAP4) -> list[bytes]:
        """Select folder and return all message sequence numbers."""
        conn.select(self.config.folder, readonly=True)
        _status, data = conn.search(None, "ALL")
        raw = data[0]
        if not raw:
            return []
        return cast("list[bytes]", raw.split())

    def _fetch_message(self, conn: imaplib.IMAP4, msg_id: bytes) -> bytes | None:
        """Fetch a single message by sequence number."""
        _status, data = conn.fetch(msg_id.decode(), "(RFC822)")
        if not data or data[0] is None:
            return None
        part = data[0]
        if isinstance(part, tuple):
            return part[1]
        return None

    def _parse_message(self, msg: Message, source_id: str) -> RawReceipt:
        """Convert an email Message to a RawReceipt."""
        subject = self._decode_header_value(msg.get("Subject", ""))
        sender = self._decode_header_value(msg.get("From", ""))

        date_str = msg.get("Date")
        email_date = parsedate_to_datetime(date_str) if date_str else None

        html_body, text_body, attachments = self._extract_body_and_attachments(msg)

        return RawReceipt(
            source_id=source_id,
            source_name=self.name,
            source_type="imap",
            date=email_date or datetime.now(tz=UTC),
            subject=subject,
            sender=sender,
            html_body=html_body,
            text_body=text_body,
            attachments=attachments,
        )

    @staticmethod
    def _get_message_id(msg: Message) -> str:
        """Extract a unique identifier for the message.

        Uses the Message-ID header if present; falls back to a hash
        of subject + date + sender.
        """
        message_id = msg.get("Message-ID")
        if message_id:
            return message_id.strip()

        subject = msg.get("Subject", "")
        date = msg.get("Date", "")
        sender = msg.get("From", "")
        key = f"{subject}|{date}|{sender}"
        return hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def _decode_header_value(value: str | None) -> str:
        """Decode an RFC 2047 encoded header value."""
        if not value:
            return ""
        parts = decode_header(value)
        decoded_parts: list[str] = []
        for data, charset in parts:
            if isinstance(data, bytes):
                decoded_parts.append(data.decode(charset or "utf-8", errors="replace"))
            else:
                decoded_parts.append(data)
        return "".join(decoded_parts)

    @staticmethod
    def _extract_body_and_attachments(
        msg: Message,
    ) -> tuple[str | None, str | None, list[Attachment]]:
        """Walk MIME tree and extract body content and attachments."""
        html_body: str | None = None
        text_body: str | None = None
        attachments: list[Attachment] = []

        if not msg.is_multipart():
            content_type = msg.get_content_type()
            charset = msg.get_content_charset() or "utf-8"
            raw_payload = msg.get_payload(decode=True)
            if raw_payload is None:
                return None, None, []
            payload = cast("bytes", raw_payload)
            if content_type == "text/html":
                html_body = payload.decode(charset, errors="replace")
            elif content_type == "text/plain":
                text_body = payload.decode(charset, errors="replace")
            return html_body, text_body, attachments

        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            # Skip multipart containers
            if part.get_content_maintype() == "multipart":
                continue

            raw_payload = part.get_payload(decode=True)
            if raw_payload is None:
                continue
            payload = cast("bytes", raw_payload)

            # Attachment if it has a filename or explicit disposition
            filename = part.get_filename()
            is_attachment = bool(filename) or "attachment" in disposition.lower()

            if is_attachment:
                attachments.append(
                    Attachment(
                        filename=filename or "unnamed",
                        content_type=content_type,
                        data=payload,
                    )
                )
            elif content_type == "text/html" and html_body is None:
                charset = part.get_content_charset() or "utf-8"
                html_body = payload.decode(charset, errors="replace")
            elif content_type == "text/plain" and text_body is None:
                charset = part.get_content_charset() or "utf-8"
                text_body = payload.decode(charset, errors="replace")
            elif content_type.startswith("image/"):
                # Inline images without filename
                content_id = part.get("Content-ID", "")
                name = content_id.strip("<>") if content_id else "inline-image"
                attachments.append(
                    Attachment(
                        filename=name,
                        content_type=content_type,
                        data=payload,
                    )
                )

        return html_body, text_body, attachments
