"""IMAP source adapter.

Reads incrementally: only UIDs above the stored cursor, and only the headers
needed for the source ID until the pipeline decides to fetch a body (see the
always-on ingest design, sections 2 and 3).
"""

from __future__ import annotations

import hashlib
import logging
import ssl
import time
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Self, cast

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.models import Attachment, ItemRef, RawReceipt, SourceRead

if TYPE_CHECKING:
    from email.message import Message
    from types import TracebackType

    from receipt_index.config import ImapSourceConfig
    from receipt_index.models import SyncState

logger = logging.getLogger(__name__)

_MAX_CONNECT_RETRIES = 3
# Socket timeout for every IMAP command, so a silent server raises instead of
# blocking a run (and the lock it holds) for ever.
_IMAP_TIMEOUT_SECONDS = 120.0

# Header-only fetch: enough for the source ID and the ingest_log row.
_HEADER_FETCH = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE FROM)]"
# The exact RFC 822 bytes. PEEK leaves the \Seen flag alone.
_BODY_FETCH = "BODY.PEEK[]"
_BODY_KEY = b"BODY[]"
# UIDs per header FETCH command, so a full scan stays within line limits.
_FETCH_BATCH = 500


class MessageRefusedError(Exception):
    """The server, on a healthy session, refused one message: an item error."""


def _header_bytes(data: dict[bytes, Any]) -> bytes | None:
    """Find the header-fields section in one message's FETCH response.

    Servers echo the section name with their own spacing and case, so match
    on its prefix rather than the exact key.
    """
    for key, value in data.items():
        if key.upper().startswith(b"BODY[HEADER") and isinstance(value, bytes):
            return value
    return None


class ImapAdapter:
    """Read new receipts from an IMAP folder."""

    source_type = "imap"

    def __init__(self, config: ImapSourceConfig) -> None:
        self.config = config
        self.name = config.name
        self._client: IMAPClient | None = None
        # The folder's UIDVALIDITY as list_new saw it: the UIDs in its refs
        # mean something only while it is unchanged.
        self._uidvalidity: int | None = None
        # Whether the last reconnect logged in and re-selected the folder, so
        # an error after it is the message's, not the session's.
        self._selected_ok = False

    @property
    def folder(self) -> str:
        """The folder the sync-state row is keyed on."""
        return self.config.folder

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Log out, if connected."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.logout()
        except Exception:
            logger.debug("Error during IMAP logout", exc_info=True)

    def _session(self) -> IMAPClient:
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _connect(self) -> IMAPClient:
        """Establish an IMAP connection and authenticate.

        TLS is verified: the default context checks the certificate and the
        host name. Retries up to 3 times with exponential backoff on
        connection errors; authentication and protocol errors aren't retried.
        """
        last_err: Exception | None = None
        for attempt in range(_MAX_CONNECT_RETRIES):
            try:
                if self.config.use_ssl:
                    client = IMAPClient(
                        self.config.host,
                        port=self.config.port,
                        ssl=True,
                        ssl_context=ssl.create_default_context(),
                        timeout=_IMAP_TIMEOUT_SECONDS,
                    )
                else:
                    client = IMAPClient(
                        self.config.host,
                        port=self.config.port,
                        ssl=False,
                        timeout=_IMAP_TIMEOUT_SECONDS,
                    )
                client.login(self.config.username, self.config.password)
                return client
            except IMAPClientError:
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

    # -- reading ---------------------------------------------------------

    def list_new(self, state: SyncState | None) -> SourceRead:
        """List messages above the stored cursor, from their headers only.

        No stored UIDVALIDITY is a first run, and a different one is a reset.
        Both start from UID 0, which makes this a full header scan.
        """
        client = self._session()
        info = client.select_folder(self.config.folder, readonly=True)  # EXAMINE
        uidvalidity = int(info[b"UIDVALIDITY"])
        self._uidvalidity = uidvalidity

        reset: str | None = None
        start_uid = 0
        if state is not None and state.imap_uidvalidity is not None:
            if state.imap_uidvalidity != uidvalidity:
                logger.warning(
                    "UIDVALIDITY of %s on source %s changed (%d -> %d): "
                    "rescanning headers",
                    self.config.folder,
                    self.name,
                    state.imap_uidvalidity,
                    uidvalidity,
                )
                reset = "uidvalidity changed"
            else:
                start_uid = state.imap_last_uid or 0

        # RFC 3501: "n:*" also matches the highest UID when n is above it,
        # so filter client-side.
        found = client.search(["UID", f"{start_uid + 1}:*"])
        uids = sorted(int(u) for u in found if int(u) > start_uid)

        items: list[ItemRef] = []
        for i in range(0, len(uids), _FETCH_BATCH):
            batch = uids[i : i + _FETCH_BATCH]
            response, refused = self._fetch_headers(client, batch)
            for uid in batch:
                if uid in refused:
                    items.append(self._unreadable_ref(uid, uidvalidity, refused[uid]))
                    continue
                data = response.get(uid)
                headers = _header_bytes(data) if data else None
                if headers is None:
                    logger.warning("No headers returned for UID %d; skipping", uid)
                    continue
                items.append(self._ref(uid, headers))

        return SourceRead(
            items=items,
            reset=reset,
            uidvalidity=uidvalidity,
            start_uid=start_uid,
            searched_uids=uids,
        )

    @staticmethod
    def _fetch_headers(
        client: IMAPClient, batch: list[int]
    ) -> tuple[dict[int, dict[bytes, Any]], dict[int, str]]:
        """Fetch a batch's headers; fall back to one UID at a time on a NO.

        A server that refuses one message (a tagged NO or BAD, for example a
        corrupt message file) fails the whole batch command. Fetching each
        UID alone keeps the others readable and names the refused ones. A
        dropped connection still propagates: that is the source's problem.
        """
        try:
            response: dict[int, dict[bytes, Any]] = client.fetch(batch, [_HEADER_FETCH])
            return response, {}
        except IMAPClientAbortError:
            raise
        except IMAPClientError as exc:
            logger.warning(
                "Header fetch of %d UIDs refused (%s); fetching one at a time",
                len(batch),
                exc,
            )
        response = {}
        refused: dict[int, str] = {}
        for uid in batch:
            try:
                response.update(client.fetch([uid], [_HEADER_FETCH]))
            except IMAPClientAbortError:
                raise
            except IMAPClientError as exc:
                refused[uid] = str(exc)
        return response, refused

    def _unreadable_ref(self, uid: int, uidvalidity: int, reason: str) -> ItemRef:
        """A ref for a message whose headers the server refuses.

        It has no Message-ID to key on, so its ID names the folder position.
        Recording it as failed lets the cursor pass it rather than stall the
        source on it every run.
        """
        logger.warning("Server refused the headers of UID %d: %s", uid, reason)
        return ItemRef(
            source_id=f"imap-uid:{self.config.folder}:{uidvalidity}:{uid}",
            source_type="imap",
            imap_uid=uid,
            unreadable=f"server refused the message headers: {reason}",
        )

    def _ref(self, uid: int, headers: bytes) -> ItemRef:
        """Build a ref from header bytes. Decoding problems never drop it."""
        msg = message_from_bytes(headers)
        date: datetime | None = None
        date_str = msg.get("Date")
        if date_str:
            try:
                date = parsedate_to_datetime(str(date_str))
            except (TypeError, ValueError):
                date = None
        return ItemRef(
            source_id=self._get_message_id(msg),
            source_type="imap",
            subject=self._decode_header_value(msg.get("Subject", "")),
            sender=self._decode_header_value(msg.get("From", "")),
            date=date,
            imap_uid=uid,
        )

    def fetch(self, ref: ItemRef) -> RawReceipt | None:
        """Download and parse one message. None if it's no longer there.

        The receipt keeps the ref's source ID, so the item's started row and
        its outcome row share one ID.
        """
        if ref.imap_uid is None:
            raise ValueError(f"IMAP ref without a UID: {ref.source_id}")
        if ref.unreadable is not None:
            raise MessageRefusedError(ref.unreadable)
        response = self._fetch_body(ref.imap_uid)
        data = response.get(ref.imap_uid)
        raw = data.get(_BODY_KEY) if data else None
        if not isinstance(raw, bytes):
            return None
        return self._parse_message(message_from_bytes(raw), ref.source_id)

    def _fetch_body(self, uid: int) -> dict[int, dict[bytes, Any]]:
        """Fetch one body, reconnecting once if the connection fails.

        A first failure of any kind gets one reconnect, which re-selects the
        folder and checks its UIDVALIDITY (a message that has gone comes back
        as an empty response, not an error). After that:

        - a dropped connection, or a reconnect that fails, is the server's
          problem: ``SourceUnavailableError``;
        - a tagged NO or BAD on the healthy new session is this message's
          problem: ``MessageRefusedError``, an item error.
        """
        try:
            result: dict[int, dict[bytes, Any]] = self._session().fetch(
                [uid], [_BODY_FETCH]
            )
            return result
        except (OSError, IMAPClientError) as exc:
            logger.warning("IMAP fetch failed (%s); reconnecting once", exc)
            self._drop_client()
        try:
            self._reselect()
            result = self._session().fetch([uid], [_BODY_FETCH])
            return result
        except (OSError, IMAPClientAbortError, ConnectionError) as exc:
            self._drop_client()
            raise SourceUnavailableError(
                f"IMAP connection to {self.config.host} failed: {exc}"
            ) from exc
        except IMAPClientError as exc:
            if self._client is not None and self._selected_ok:
                raise MessageRefusedError(f"server refused UID {uid}: {exc}") from exc
            self._drop_client()
            raise SourceUnavailableError(
                f"IMAP connection to {self.config.host} failed: {exc}"
            ) from exc

    def _drop_client(self) -> None:
        """Forget a broken connection, so the next session reconnects."""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                logger.debug("Error closing a broken IMAP connection", exc_info=True)

    def _reselect(self) -> None:
        """Open a fresh session on the folder list_new read.

        A changed UIDVALIDITY means the UIDs in hand point at other messages:
        stop the source, and the next run's list_new resets the cursor.
        """
        self._selected_ok = False
        info = self._session().select_folder(self.config.folder, readonly=True)
        uidvalidity = int(info[b"UIDVALIDITY"])
        if self._uidvalidity is not None and uidvalidity != self._uidvalidity:
            raise SourceUnavailableError(
                f"UIDVALIDITY of {self.config.folder} changed during the run "
                f"({self._uidvalidity} -> {uidvalidity})"
            )
        self._selected_ok = True

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
            # 8-bit bytes come back as an email.header.Header, not a str.
            return str(message_id).strip()

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
        try:
            parts = decode_header(value)
            decoded_parts: list[str] = []
            for data, charset in parts:
                if isinstance(data, bytes):
                    decoded_parts.append(
                        data.decode(charset or "utf-8", errors="replace")
                    )
                else:
                    decoded_parts.append(data)
            return "".join(decoded_parts)
        except (LookupError, UnicodeError, ValueError):
            # An unknown charset or a malformed encoded word: keep the raw text.
            return str(value)

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
