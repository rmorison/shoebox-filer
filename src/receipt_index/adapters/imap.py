"""IMAP source adapter.

Reads incrementally: only UIDs above the stored cursor, and only the headers
needed for the source ID until the pipeline decides to fetch a body (see the
always-on ingest design, sections 2 and 3).
"""

from __future__ import annotations

import hashlib
import logging
import re
import ssl
import time
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Self, cast

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.models import Attachment, ItemRef, RawReceipt, SourceRead

if TYPE_CHECKING:
    from datetime import date
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


# RFC 5530 response codes that describe the session or the mailbox, not one
# message: a NO carrying one is an outage, never a refused message.
# SERVERBUG and LIMIT are left out on purpose: Dovecot uses them for a FETCH
# that fails on one message (a corrupt file), so they stay item errors. A
# mailbox-wide failure under them is caught by the all-refused-batch rule.
_SESSION_CODES = ("UNAVAILABLE", "INUSE", "OVERQUOTA")
# Gmail's refusal once an account passes its IMAP command or download
# allowance, which it may send without a response code. It ends the run,
# so a throttled lookup is never taken for a missing message.
_THROTTLE_TEXT = "EXCEEDED COMMAND OR BANDWIDTH LIMITS"


def _is_session_refusal(exc: Exception) -> bool:
    text = str(exc).upper()
    return _THROTTLE_TEXT in text or any(f"[{code}" in text for code in _SESSION_CODES)


# The fallback ID of a message with no Message-ID: a SHA-256 hex digest.
_HASH_ID = re.compile(r"[0-9a-f]{64}")


def _message_id_search(source_id: str) -> str | None:
    """The ``SEARCH`` criteria for a Message-ID, or None if it can't be searched.

    The ID is sent as an IMAP quoted string (RFC 3501: backslash and quote
    escaped; no CR, LF or 8-bit characters). The criteria are passed to
    IMAPClient as one string, which it sends as is; as a list item it would
    quote the string again, and it leaves some specials such as ``(`` bare.
    Fallback-hash IDs (no ``Message-ID``) and ``imap-uid:`` IDs return None.
    """
    if source_id.startswith("imap-uid:") or _HASH_ID.fullmatch(source_id):
        return None
    if not source_id.isascii() or any(c in source_id for c in "\r\n\0"):
        return None
    escaped = source_id.replace("\\", "\\\\").replace('"', '\\"')
    return f'HEADER Message-ID "{escaped}"'


def can_search_by_id(source_id: str) -> bool:
    """Whether ``fetch_by_id`` can look this source ID up at all."""
    return _message_id_search(source_id) is not None


def _range_criteria(since: date | None, until: date | None) -> list[Any]:
    """``SEARCH`` criteria for arrival days ``since`` to ``until``, inclusive.

    IMAP's ``BEFORE`` excludes its own day, so it gets the day after
    ``until``. Both use the server's internal (arrival) date.
    """
    criteria: list[Any] = []
    if since is not None:
        criteria += ["SINCE", since]
    if until is not None:
        criteria += ["BEFORE", until + timedelta(days=1)]
    return criteria or ["ALL"]


def _header_bytes(data: dict[bytes, Any]) -> bytes | None:
    """Find the header-fields section in one message's FETCH response.

    Servers echo the section name with their own spacing and case, so match
    on its prefix rather than the exact key.
    """
    for key, value in data.items():
        if key.upper().startswith(b"BODY[HEADER") and isinstance(value, bytes):
            return value
    return None


def connect_imap(config: ImapSourceConfig) -> IMAPClient:
    """Establish an IMAP connection and authenticate.

    Shared by the adapter and ``serve``'s IDLE watcher. TLS is verified: the
    default context checks the certificate and the host name. Retries up to 3
    times with exponential backoff on connection errors; authentication and
    protocol errors aren't retried.
    """
    last_err: Exception | None = None
    for attempt in range(_MAX_CONNECT_RETRIES):
        try:
            if config.use_ssl:
                client = IMAPClient(
                    config.host,
                    port=config.port,
                    ssl=True,
                    ssl_context=ssl.create_default_context(),
                    timeout=_IMAP_TIMEOUT_SECONDS,
                )
            else:
                client = IMAPClient(
                    config.host,
                    port=config.port,
                    ssl=False,
                    timeout=_IMAP_TIMEOUT_SECONDS,
                )
            client.login(config.username, config.password)
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
        # The folder those UIDs belong to, and the folder selected on the
        # live session (None after a reconnect, until one is selected).
        self._uid_folder: str | None = None
        self._selected: str | None = None
        # Each folder's UIDVALIDITY as list_range saw it.
        self._range_validity: dict[str, int] = {}
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
        self._selected = None
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
        return connect_imap(self.config)

    def _select(self, folder: str) -> int:
        """Select ``folder`` read-only (EXAMINE); return its UIDVALIDITY."""
        info = self._session().select_folder(folder, readonly=True)
        self._selected = folder
        return int(info[b"UIDVALIDITY"])

    def _use(self, folder: str, uidvalidity: int) -> None:
        """Record which folder's UIDs are in hand."""
        self._uid_folder = folder
        self._uidvalidity = uidvalidity

    # -- reading ---------------------------------------------------------

    def list_new(self, state: SyncState | None) -> SourceRead:
        """List messages above the stored cursor, from their headers only.

        No stored UIDVALIDITY is a first run, and a different one is a reset.
        Both start from UID 0, which makes this a full header scan.
        """
        client = self._session()
        uidvalidity = self._select(self.config.folder)
        self._use(self.config.folder, uidvalidity)
        self._range_validity[self.config.folder] = uidvalidity

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

    def list_range(self, since: date | None, until: date | None) -> list[ItemRef]:
        """List the messages that arrived from ``since`` to ``until``, inclusive.

        Searches every folder ``fetch_by_id`` searches, from headers only,
        and never touches the sync state. A Message-ID found twice is listed
        once, from the first folder searched. Each ref names its folder.
        """
        criteria = _range_criteria(since, until)
        items: list[ItemRef] = []
        seen: set[str] = set()
        for folder in self._lookup_folders():
            client = self._session()
            uidvalidity = self._select(folder)
            self._use(folder, uidvalidity)
            self._range_validity[folder] = uidvalidity
            uids = sorted(int(u) for u in client.search(criteria))
            for i in range(0, len(uids), _FETCH_BATCH):
                batch = uids[i : i + _FETCH_BATCH]
                response, refused = self._fetch_headers(client, batch)
                for uid in batch:
                    if uid in refused:
                        ref = self._unreadable_ref(
                            uid, uidvalidity, refused[uid], folder
                        )
                    else:
                        data = response.get(uid)
                        headers = _header_bytes(data) if data else None
                        if headers is None:
                            logger.warning(
                                "No headers returned for UID %d; skipping", uid
                            )
                            continue
                        ref = self._ref(uid, headers)
                    if ref.source_id in seen:
                        continue
                    seen.add(ref.source_id)
                    ref.imap_folder = folder
                    items.append(ref)
        return items

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
            if _is_session_refusal(exc):
                raise SourceUnavailableError(f"IMAP server refused: {exc}") from exc
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
                if _is_session_refusal(exc):
                    raise SourceUnavailableError(f"IMAP server refused: {exc}") from exc
                refused[uid] = str(exc)
        if len(batch) > 1 and len(refused) == len(batch):
            # Every message refused at once points at the mailbox, not at
            # each message: stop the source rather than fail them all.
            raise SourceUnavailableError(
                f"IMAP server refused every message in a batch of {len(batch)}"
            )
        return response, refused

    def _unreadable_ref(
        self, uid: int, uidvalidity: int, reason: str, folder: str | None = None
    ) -> ItemRef:
        """A ref for a message whose headers the server refuses.

        It has no Message-ID to key on, so its ID names the folder position.
        Recording it as failed lets the cursor pass it rather than stall the
        source on it every run.
        """
        logger.warning("Server refused the headers of UID %d: %s", uid, reason)
        return ItemRef(
            source_id=(
                f"imap-uid:{self.name}:{folder or self.config.folder}:"
                f"{uidvalidity}:{uid}"
            ),
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
        folder = ref.imap_folder or self.config.folder
        if folder != self._selected:
            self._select_listed(folder)
        response = self._fetch_body(ref.imap_uid)
        data = response.get(ref.imap_uid)
        raw = data.get(_BODY_KEY) if data else None
        if not isinstance(raw, bytes):
            return None
        return self._parse_message(raw, ref.source_id)

    def _select_listed(self, folder: str) -> None:
        """Re-select the folder a ref was listed from, checking its UIDVALIDITY."""
        try:
            uidvalidity = self._select(folder)
        except (OSError, IMAPClientError, ConnectionError) as exc:
            self._drop_client()
            raise SourceUnavailableError(
                f"IMAP connection to {self.config.host} failed: {exc}"
            ) from exc
        listed = self._range_validity.get(folder)
        if listed is not None and listed != uidvalidity:
            raise SourceUnavailableError(
                f"UIDVALIDITY of {folder} changed during the run "
                f"({listed} -> {uidvalidity})"
            )
        self._use(folder, uidvalidity)

    def _lookup_folders(self) -> list[str]:
        """The folders ``fetch_by_id`` searches, in order.

        Only the watched folder for now; the archive and needs-attention
        folders join it when the configuration has them.
        """
        return [self.config.folder]

    def fetch_by_id(self, source_id: str) -> RawReceipt | None:
        """Find one message by its Message-ID and download it.

        None unless exactly one message, across the searched folders, has
        that exact Message-ID (it is set by the sender and isn't guaranteed
        unique), and for IDs that can't be searched for.
        """
        criteria = _message_id_search(source_id)
        if criteria is None:
            return None
        # Everything up to the body fetch: SEARCH, the exact-ID check and the
        # re-select. A dropped connection anywhere here is the source's
        # problem, never this message's (the body fetch handles its own).
        try:
            found = self._find_exact(criteria, source_id)
        except (OSError, IMAPClientAbortError, ConnectionError) as exc:
            self._drop_client()
            raise SourceUnavailableError(
                f"IMAP connection to {self.config.host} failed: {exc}"
            ) from exc
        except IMAPClientError as exc:
            if _is_session_refusal(exc) or self._client is None:
                self._drop_client()
                raise SourceUnavailableError(f"IMAP server refused: {exc}") from exc
            raise MessageRefusedError(
                f"server refused the search for {source_id}: {exc}"
            ) from exc
        if found is None:
            return None
        uid = found
        response = self._fetch_body(uid)
        data = response.get(uid)
        raw = data.get(_BODY_KEY) if data else None
        if not isinstance(raw, bytes):
            return None
        return self._parse_message(raw, source_id)

    def _find_exact(self, criteria: str, source_id: str) -> int | None:
        """The UID of the one message with exactly this Message-ID, selected.

        None when none or several match. Leaves its folder selected.
        """
        matches: list[tuple[str, int, int]] = []
        for folder in self._lookup_folders():
            uidvalidity = self._select(folder)
            for hit in self._session().search(criteria):
                matches.append((folder, uidvalidity, int(hit)))

        # SEARCH HEADER matches substrings: keep exact matches only.
        exact = [m for m in matches if self._has_message_id(m, source_id)]
        if len(exact) != 1:
            if len(exact) > 1:
                logger.warning(
                    "%d messages share Message-ID %s; not re-fetching",
                    len(exact),
                    source_id,
                )
            return None
        folder, uidvalidity, uid = exact[0]
        self._select(folder)
        self._use(folder, uidvalidity)
        return uid

    def _has_message_id(self, match: tuple[str, int, int], source_id: str) -> bool:
        """Whether a search hit's own Message-ID is exactly ``source_id``."""
        folder, _uidvalidity, uid = match
        self._select(folder)
        response, refused = self._fetch_headers(self._session(), [uid])
        if uid in refused:
            # A refusal isn't an answer: never let it read as "not this one",
            # or a present message would be taken for a missing one.
            raise MessageRefusedError(
                f"server refused the headers of UID {uid}: {refused[uid]}"
            )
        data = response.get(uid)
        headers = _header_bytes(data) if data else None
        if headers is None:
            return False
        return self._get_message_id(message_from_bytes(headers)) == source_id

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
            if (
                self._client is not None
                and self._selected_ok
                and not _is_session_refusal(exc)
            ):
                raise MessageRefusedError(f"server refused UID {uid}: {exc}") from exc
            self._drop_client()
            raise SourceUnavailableError(
                f"IMAP connection to {self.config.host} failed: {exc}"
            ) from exc

    def _drop_client(self) -> None:
        """Forget a broken connection, so the next session reconnects."""
        client, self._client = self._client, None
        self._selected = None
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                logger.debug("Error closing a broken IMAP connection", exc_info=True)

    def _reselect(self) -> None:
        """Open a fresh session on the folder whose UIDs are in hand.

        A changed UIDVALIDITY means the UIDs in hand point at other messages:
        stop the source, and the next run's list_new resets the cursor.
        """
        self._selected_ok = False
        folder = self._uid_folder or self.config.folder
        uidvalidity = self._select(folder)
        if self._uidvalidity is not None and uidvalidity != self._uidvalidity:
            raise SourceUnavailableError(
                f"UIDVALIDITY of {folder} changed during the run "
                f"({self._uidvalidity} -> {uidvalidity})"
            )
        self._selected_ok = True

    def _parse_message(self, raw: bytes, source_id: str) -> RawReceipt:
        """Parse a fetched message into a RawReceipt.

        The receipt keeps ``raw`` as its ``raw_bytes``, so the stored original
        is byte for byte what extraction read.
        """
        msg = message_from_bytes(raw)
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
            raw_bytes=raw,
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
                        mime_filename=filename or None,
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
