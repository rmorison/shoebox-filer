"""Tests for receipt_index.adapters.imap."""

from __future__ import annotations

import hashlib
import re
import ssl
from datetime import UTC, datetime
from email import message_from_bytes
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError

from receipt_index.adapters.base import SourceAdapter, SourceUnavailableError
from receipt_index.adapters.imap import ImapAdapter, MessageRefusedError
from receipt_index.models import ItemRef, SyncState

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


class TestDecodeHeaderFailSoft:
    def test_unknown_charset_keeps_raw_text(self) -> None:
        raw = "=?x-no-such-charset?q?Receipt?="
        assert ImapAdapter._decode_header_value(raw) == raw


# ---------------------------------------------------------------------------
# A fake imapclient.IMAPClient
# ---------------------------------------------------------------------------

_WANTED = (b"message-id", b"subject", b"date", b"from")


def _header_fields(message: bytes) -> bytes:
    """What a server returns for BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE FROM)].

    The header lines, folded continuations included, exactly as stored, then
    a blank line.
    """
    head = re.split(rb"\r?\n\r?\n", message, maxsplit=1)[0]
    lines = re.split(rb"(?<=\n)", head + b"\n")
    out: list[bytes] = []
    keep = False
    for line in lines:
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            if keep:
                out.append(line)
            continue
        keep = line.split(b":", 1)[0].strip().lower() in _WANTED
        if keep:
            out.append(line)
    return b"".join(out) + b"\n"


class FakeIMAPClient:
    """Enough of IMAPClient for the adapter: EXAMINE, UID SEARCH and FETCH."""

    def __init__(self, messages: dict[int, bytes], uidvalidity: int = 7) -> None:
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.selected: tuple[str, bool] | None = None
        self.searches: list[list[str]] = []
        self.fetches: list[tuple[list[int], list[str]]] = []
        self.logged_out = False
        self.drop_headers: set[int] = set()
        # UIDs the server answers with a tagged NO (e.g. a corrupt file).
        self.refuse: set[int] = set()

    def login(self, username: str, password: str) -> None:
        pass

    def logout(self) -> None:
        self.logged_out = True

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]:
        self.selected = (folder, readonly)
        return {b"UIDVALIDITY": self.uidvalidity, b"EXISTS": len(self.messages)}

    def search(self, criteria: list[str]) -> list[int]:
        self.searches.append(criteria)
        assert criteria[0] == "UID"
        low = int(criteria[1].split(":")[0])
        uids = sorted(u for u in self.messages if u >= low)
        if not uids and self.messages:
            uids = [max(self.messages)]  # RFC 3501: n:* matches the highest UID
        return uids

    def fetch(self, uids: list[int], items: list[str]) -> dict[int, dict[bytes, Any]]:
        if self.selected is None:  # a real server answers BAD
            raise IMAPClientError("FETCH command error: BAD [No mailbox selected]")
        if self.refuse & set(uids):
            raise IMAPClientError("FETCH command error: NO [Message file corrupt]")
        self.fetches.append((list(uids), list(items)))
        out: dict[int, dict[bytes, Any]] = {}
        for uid in uids:
            if uid not in self.messages:
                continue
            if items == ["BODY.PEEK[]"]:
                out[uid] = {b"BODY[]": self.messages[uid], b"SEQ": uid}
            else:
                if uid in self.drop_headers:
                    out[uid] = {b"SEQ": uid}
                    continue
                key = b"BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE FROM)]"
                out[uid] = {key: _header_fields(self.messages[uid]), b"SEQ": uid}
        return out

    def body_fetches(self) -> list[int]:
        return [
            u for uids, items in self.fetches if items == ["BODY.PEEK[]"] for u in uids
        ]


def _state(uidvalidity: int | None, last_uid: int | None) -> SyncState:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SyncState.model_validate(
        {
            "id": "019572a0-0000-7000-8000-000000000001",
            "source_name": "test-imap",
            "source_type": "imap",
            "folder": "INBOX",
            "imap_uidvalidity": uidvalidity,
            "imap_last_uid": last_uid,
            "created_at": now,
            "updated_at": now,
        }
    )


def _mail(n: int, **kw: Any) -> bytes:
    return _make_simple_email(
        message_id=f"<msg-{n}@example.com>", subject=f"Receipt {n}", **kw
    )


@pytest.fixture
def fake() -> FakeIMAPClient:
    return FakeIMAPClient({1: _mail(1), 2: _mail(2), 5: _mail(5)})


@pytest.fixture
def adapter(imap_config: ImapSourceConfig, fake: FakeIMAPClient) -> ImapAdapter:
    a = ImapAdapter(imap_config)
    a._client = fake  # type: ignore[assignment]
    fake.select_folder("INBOX", readonly=True)  # as list_new would
    a._uidvalidity = fake.uidvalidity
    return a


class TestConnect:
    @patch("receipt_index.adapters.imap.IMAPClient")
    def test_tls_is_verified(
        self, mock_client: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        ImapAdapter(imap_config)._connect()

        _, kwargs = mock_client.call_args
        assert kwargs["ssl"] is True
        ctx = kwargs["ssl_context"]
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True
        assert kwargs["timeout"] == 120.0
        mock_client.return_value.login.assert_called_once_with(
            "test@example.com",
            "secret",  # pragma: allowlist secret
        )

    @patch("receipt_index.adapters.imap.IMAPClient")
    def test_plain_imap_when_ssl_disabled(
        self, mock_client: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        cfg = imap_config.model_copy(update={"use_ssl": False, "port": 143})
        ImapAdapter(cfg)._connect()
        args, kwargs = mock_client.call_args
        assert args == ("imap.example.com",)
        assert kwargs == {"port": 143, "ssl": False, "timeout": 120.0}

    @patch("receipt_index.adapters.imap.time.sleep")
    @patch("receipt_index.adapters.imap.IMAPClient")
    def test_retries_on_connection_failure(
        self,
        mock_client: MagicMock,
        mock_sleep: MagicMock,
        imap_config: ImapSourceConfig,
    ) -> None:
        mock_client.side_effect = [OSError("refused"), MagicMock()]
        ImapAdapter(imap_config)._connect()
        assert mock_client.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch("receipt_index.adapters.imap.time.sleep")
    @patch("receipt_index.adapters.imap.IMAPClient")
    def test_raises_after_max_retries(
        self,
        mock_client: MagicMock,
        mock_sleep: MagicMock,
        imap_config: ImapSourceConfig,
    ) -> None:
        mock_client.side_effect = OSError("refused")
        with pytest.raises(ConnectionError, match="3 attempts"):
            ImapAdapter(imap_config)._connect()

    @patch("receipt_index.adapters.imap.IMAPClient")
    def test_login_error_not_retried(
        self, mock_client: MagicMock, imap_config: ImapSourceConfig
    ) -> None:
        mock_client.return_value.login.side_effect = LoginError("auth fail")
        with pytest.raises(LoginError, match="auth fail"):
            ImapAdapter(imap_config)._connect()
        assert mock_client.call_count == 1

    def test_close_logs_out(self, adapter: ImapAdapter, fake: FakeIMAPClient) -> None:
        with adapter:
            pass
        assert fake.logged_out

    def test_conforms_to_source_adapter_protocol(
        self, imap_config: ImapSourceConfig
    ) -> None:
        assert isinstance(ImapAdapter(imap_config), SourceAdapter)


class TestListNew:
    def test_first_run_is_a_full_header_scan(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        read = adapter.list_new(None)

        assert fake.selected == ("INBOX", True)  # EXAMINE: read-only
        assert fake.searches == [["UID", "1:*"]]
        assert [r.imap_uid for r in read.items] == [1, 2, 5]
        assert [r.source_id for r in read.items] == [
            "<msg-1@example.com>",
            "<msg-2@example.com>",
            "<msg-5@example.com>",
        ]
        assert read.items[0].subject == "Receipt 1"
        assert read.reset is None
        assert read.uidvalidity == 7
        assert read.start_uid == 0
        assert read.searched_uids == [1, 2, 5]
        assert fake.body_fetches() == []

    def test_incremental_reads_only_above_last_uid(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        read = adapter.list_new(_state(7, 2))

        assert fake.searches == [["UID", "3:*"]]
        assert [r.imap_uid for r in read.items] == [5]
        assert read.start_uid == 2
        assert read.reset is None

    def test_n_star_quirk_is_filtered(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        # Nothing above UID 5: the server still answers "6:*" with UID 5.
        read = adapter.list_new(_state(7, 5))

        assert read.items == []
        assert read.searched_uids == []
        assert fake.fetches == []

    def test_changed_uidvalidity_resets_and_rescans(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        fake.uidvalidity = 99
        read = adapter.list_new(_state(7, 5))

        assert read.reset == "uidvalidity changed"
        assert read.uidvalidity == 99
        assert read.start_uid == 0
        assert fake.searches == [["UID", "1:*"]]
        assert [r.imap_uid for r in read.items] == [1, 2, 5]
        assert fake.body_fetches() == []

    def test_row_without_uidvalidity_is_a_first_run_not_a_reset(
        self, adapter: ImapAdapter
    ) -> None:
        read = adapter.list_new(_state(None, None))
        assert read.reset is None
        assert read.start_uid == 0

    def test_uid_without_headers_has_no_ref_but_is_searched(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        fake.drop_headers = {2}
        read = adapter.list_new(None)
        assert [r.imap_uid for r in read.items] == [1, 5]
        assert read.searched_uids == [1, 2, 5]

    def test_unknown_charset_subject_still_gives_a_ref(
        self, imap_config: ImapSourceConfig
    ) -> None:
        msg = (
            b"Message-ID: <odd@example.com>\r\n"
            b"Subject: =?x-no-such-charset?q?Receipt?=\r\n"
            b"From: shop@example.com\r\n"
            b"Date: not a date\r\n\r\nbody\r\n"
        )
        fake = FakeIMAPClient({3: msg})
        a = ImapAdapter(imap_config)
        a._client = fake  # type: ignore[assignment]

        (ref,) = a.list_new(None).items
        assert ref.source_id == "<odd@example.com>"
        assert ref.subject == "=?x-no-such-charset?q?Receipt?="
        assert ref.date is None

    @pytest.mark.parametrize(
        "message",
        [
            _make_simple_email(message_id="<with-id@example.com>"),
            _make_simple_email(message_id=None, subject="No ID here"),
            _make_multipart_email(
                subject="A very long subject line " * 6,
                message_id="<folded@example.com>",
            ),
            (
                b"Subject: Folded\r\n continued subject\r\n"
                b"From: Shop\r\n <shop@example.com>\r\n"
                b"Date: Mon, 15 Jun 2025 10:30:00 +0000\r\n"
                b"X-Other: ignored\r\n\r\nbody\r\n"
            ),
        ],
        ids=["message-id", "fallback-hash", "folded-multipart", "folded-no-id"],
    )
    def test_header_only_id_equals_full_message_id(
        self, imap_config: ImapSourceConfig, message: bytes
    ) -> None:
        fake = FakeIMAPClient({1: message})
        a = ImapAdapter(imap_config)
        a._client = fake  # type: ignore[assignment]

        (ref,) = a.list_new(None).items
        full_id = ImapAdapter._get_message_id(message_from_bytes(message))
        assert ref.source_id == full_id


class TestFetch:
    def test_fetches_only_the_given_uid_body(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        refs = adapter.list_new(None).items
        raw = adapter.fetch(refs[1])

        assert fake.body_fetches() == [2]
        assert raw is not None
        assert raw.source_id == "<msg-2@example.com>"
        assert raw.subject == "Receipt 2"
        assert raw.source_name == "test-imap"
        assert raw.source_type == "imap"
        assert raw.text_body == "Hello, World!"

    def test_keeps_the_ref_source_id(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        ref = ItemRef(source_id="from-the-ref", source_type="imap", imap_uid=1)
        raw = adapter.fetch(ref)
        assert raw is not None
        assert raw.source_id == "from-the-ref"

    def test_gone_message_returns_none(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        ref = ItemRef(source_id="<x@example.com>", source_type="imap", imap_uid=42)
        assert adapter.fetch(ref) is None

    def test_parses_attachments(self, imap_config: ImapSourceConfig) -> None:
        msg = _make_multipart_email(
            attachments=[("receipt.pdf", "application/pdf", b"%PDF-1.4 fake")]
        )
        fake = FakeIMAPClient({1: msg})
        a = ImapAdapter(imap_config)
        a._client = fake  # type: ignore[assignment]
        (ref,) = a.list_new(None).items
        raw = a.fetch(ref)
        assert raw is not None
        assert [x.filename for x in raw.attachments] == ["receipt.pdf"]


class TestTransportErrors:
    def _broken(self, error: Exception) -> FakeIMAPClient:
        broken = FakeIMAPClient({1: _mail(1)})
        broken.selected = ("INBOX", True)

        def fail(*_: Any, **__: Any) -> Any:
            raise error

        broken.fetch = fail  # type: ignore[method-assign]
        return broken

    @pytest.mark.parametrize(
        "error",
        [
            OSError("connection reset"),
            IMAPClientAbortError("socket error: EOF"),
            IMAPClientError("FETCH command error: BAD"),
        ],
        ids=["oserror", "abort", "protocol-error"],
    )
    def test_reconnects_once_after_a_dropped_connection(
        self, imap_config: ImapSourceConfig, error: Exception
    ) -> None:
        fresh = FakeIMAPClient({1: _mail(1)})
        adapter = ImapAdapter(imap_config)
        adapter._client = self._broken(error)  # type: ignore[assignment]
        ref = ItemRef(source_id="<msg-1@example.com>", source_type="imap", imap_uid=1)

        with patch.object(adapter, "_connect", return_value=fresh) as connect:
            raw = adapter.fetch(ref)

        connect.assert_called_once()
        assert raw is not None
        assert raw.source_id == "<msg-1@example.com>"
        assert adapter._client is fresh
        assert fresh.selected == ("INBOX", True)  # re-selected, read-only

    def test_changed_uidvalidity_on_reconnect_is_a_source_outage(
        self, imap_config: ImapSourceConfig
    ) -> None:
        adapter = ImapAdapter(imap_config)
        adapter._client = self._broken(OSError("reset"))  # type: ignore[assignment]
        adapter._uidvalidity = 7
        rebuilt = FakeIMAPClient({1: _mail(1)}, uidvalidity=8)
        ref = ItemRef(source_id="<msg-1@example.com>", source_type="imap", imap_uid=1)

        with (
            patch.object(adapter, "_connect", return_value=rebuilt),
            pytest.raises(SourceUnavailableError, match="UIDVALIDITY"),
        ):
            adapter.fetch(ref)
        assert rebuilt.body_fetches() == []

    def test_login_refused_on_reconnect_is_a_source_outage(
        self, imap_config: ImapSourceConfig
    ) -> None:
        adapter = ImapAdapter(imap_config)
        adapter._client = self._broken(OSError("reset"))  # type: ignore[assignment]
        ref = ItemRef(source_id="<msg-1@example.com>", source_type="imap", imap_uid=1)
        with (
            patch.object(adapter, "_connect", side_effect=LoginError("too many")),
            pytest.raises(SourceUnavailableError, match="too many"),
        ):
            adapter.fetch(ref)

    def test_second_failure_is_a_source_outage(
        self, imap_config: ImapSourceConfig
    ) -> None:
        adapter = ImapAdapter(imap_config)
        adapter._client = self._broken(OSError("reset"))  # type: ignore[assignment]
        ref = ItemRef(source_id="<msg-1@example.com>", source_type="imap", imap_uid=1)

        with (
            patch.object(
                adapter, "_connect", return_value=self._broken(OSError("reset again"))
            ),
            pytest.raises(SourceUnavailableError, match="reset again"),
        ):
            adapter.fetch(ref)
        assert adapter._client is None

    def test_a_bad_message_is_not_a_source_outage(
        self, imap_config: ImapSourceConfig
    ) -> None:
        adapter = ImapAdapter(imap_config)
        fake = FakeIMAPClient({1: _mail(1)})
        fake.selected = ("INBOX", True)
        adapter._client = fake  # type: ignore[assignment]
        ref = ItemRef(source_id="<msg-1@example.com>", source_type="imap", imap_uid=1)
        with (
            patch.object(adapter, "_parse_message", side_effect=ValueError("bad MIME")),
            pytest.raises(ValueError, match="bad MIME"),
        ):
            adapter.fetch(ref)


class TestHeaderBatches:
    def test_header_fetch_spans_several_batches(
        self, imap_config: ImapSourceConfig
    ) -> None:
        fake = FakeIMAPClient({u: _mail(u) for u in range(1, 6)})
        adapter = ImapAdapter(imap_config)
        adapter._client = fake  # type: ignore[assignment]

        with patch("receipt_index.adapters.imap._FETCH_BATCH", 2):
            read = adapter.list_new(None)

        assert [r.imap_uid for r in read.items] == [1, 2, 3, 4, 5]
        header_batches = [
            uids for uids, items in fake.fetches if items != ["BODY.PEEK[]"]
        ]
        assert header_batches == [[1, 2], [3, 4], [5]]


class TestRefusedMessages:
    """A server that refuses one message stalls nothing but that message."""

    def test_refused_body_after_a_good_reconnect_is_an_item_error(
        self, imap_config: ImapSourceConfig
    ) -> None:
        first = FakeIMAPClient({1: _mail(1)})
        first.selected = ("INBOX", True)
        first.refuse = {1}
        fresh = FakeIMAPClient({1: _mail(1)})
        fresh.refuse = {1}
        adapter = ImapAdapter(imap_config)
        adapter._client = first  # type: ignore[assignment]
        adapter._uidvalidity = 7
        ref = ItemRef(source_id="<msg-1@example.com>", source_type="imap", imap_uid=1)

        with (
            patch.object(adapter, "_connect", return_value=fresh),
            pytest.raises(MessageRefusedError, match="corrupt"),
        ):
            adapter.fetch(ref)
        assert fresh.selected == ("INBOX", True)

    def test_refused_header_batch_falls_back_one_uid_at_a_time(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        fake.refuse = {2}
        read = adapter.list_new(None)

        assert [r.imap_uid for r in read.items] == [1, 2, 5]
        good = [r for r in read.items if r.unreadable is None]
        assert [r.source_id for r in good] == [
            "<msg-1@example.com>",
            "<msg-5@example.com>",
        ]
        (bad,) = [r for r in read.items if r.unreadable is not None]
        assert bad.source_id == "imap-uid:test-imap:INBOX:7:2"
        assert "corrupt" in bad.unreadable
        with pytest.raises(MessageRefusedError):
            adapter.fetch(bad)

    def test_every_uid_refused_is_a_source_outage(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        fake.refuse = {1, 2, 5}
        with pytest.raises(SourceUnavailableError, match="every message"):
            adapter.list_new(None)

    @pytest.mark.parametrize("code", ["UNAVAILABLE", "INUSE", "OVERQUOTA"])
    def test_session_code_on_headers_is_a_source_outage(
        self, adapter: ImapAdapter, fake: FakeIMAPClient, code: str
    ) -> None:
        def refuse(*_: Any, **__: Any) -> Any:
            raise IMAPClientError(f"FETCH command error: NO [{code}] try later")

        fake.fetch = refuse  # type: ignore[method-assign]
        with pytest.raises(SourceUnavailableError, match=code):
            adapter.list_new(None)

    def test_session_code_on_body_after_reconnect_is_a_source_outage(
        self, imap_config: ImapSourceConfig
    ) -> None:
        first = FakeIMAPClient({1: _mail(1)})
        first.selected = ("INBOX", True)
        first.refuse = {1}
        fresh = FakeIMAPClient({1: _mail(1)})

        def unavailable(uids: list[int], items: list[str]) -> Any:
            raise IMAPClientError("FETCH command error: NO [UNAVAILABLE] backend down")

        fresh.fetch = unavailable  # type: ignore[method-assign]
        adapter = ImapAdapter(imap_config)
        adapter._client = first  # type: ignore[assignment]
        adapter._uidvalidity = 7
        ref = ItemRef(source_id="<msg-1@example.com>", source_type="imap", imap_uid=1)
        with (
            patch.object(adapter, "_connect", return_value=fresh),
            pytest.raises(SourceUnavailableError, match="UNAVAILABLE"),
        ):
            adapter.fetch(ref)

    @pytest.mark.parametrize("code", ["SERVERBUG", "LIMIT"])
    def test_per_message_codes_on_one_uid_stay_item_failures(
        self, adapter: ImapAdapter, fake: FakeIMAPClient, code: str
    ) -> None:
        """Dovecot answers a corrupt message's FETCH with NO [SERVERBUG]."""
        real_fetch = fake.fetch

        def fetch(uids: list[int], items: list[str]) -> Any:
            if 2 in uids:
                raise IMAPClientError(
                    f"FETCH command error: NO [{code}] Internal error"
                )
            return real_fetch(uids, items)

        fake.fetch = fetch  # type: ignore[method-assign]
        read = adapter.list_new(None)

        unreadable = [r.imap_uid for r in read.items if r.unreadable is not None]
        assert unreadable == [2]
        assert len(read.items) == 3

    def test_dropped_connection_during_headers_still_fails_the_read(
        self, adapter: ImapAdapter, fake: FakeIMAPClient
    ) -> None:
        def drop(*_: Any, **__: Any) -> Any:
            raise IMAPClientAbortError("socket error: EOF")

        fake.fetch = drop  # type: ignore[method-assign]
        with pytest.raises(IMAPClientAbortError):
            adapter.list_new(None)

    def test_cursor_passes_a_refused_message(
        self,
        imap_config: ImapSourceConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end through the pipeline with the real adapter."""
        from receipt_index import pipeline
        from tests.unit.test_pipeline import FakeRepo, _extraction

        fake = FakeIMAPClient({1: _mail(1), 2: _mail(2), 3: _mail(3)})
        fake.refuse = {2}
        adapter = ImapAdapter(imap_config)
        adapter._client = fake  # type: ignore[assignment]
        repo = FakeRepo()
        for name in (
            "get_sync_state",
            "record_poll",
            "get_processed_source_ids",
            "is_source_id_known",
            "insert_ingest_log",
            "insert_receipt",
            "save_imap_cursor",
            "save_drive_cursor",
            "item_lock",
        ):
            monkeypatch.setattr(pipeline, name, getattr(repo, name))
        monkeypatch.setattr(
            pipeline, "extract_metadata", MagicMock(return_value=_extraction())
        )
        monkeypatch.setattr(pipeline, "render_pdf", MagicMock(return_value=b"%PDF"))
        store = MagicMock()
        store.save.return_value = "x.pdf"

        result = pipeline.run_ingest(
            conn=MagicMock(),
            adapter=adapter,
            store=store,
            source_name="test-imap",
            source_type="imap",
        )

        assert result.source_error is None
        assert result.processed == 2
        assert result.failed == 1
        assert repo.statuses("imap-uid:test-imap:INBOX:7:2") == ["started", "failed"]
        assert repo.imap_saves[-1] == (7, 3, False)


class TestNonAsciiMessageId:
    def test_eight_bit_message_id_gives_a_ref(
        self, imap_config: ImapSourceConfig
    ) -> None:
        msg = (
            b"Message-ID: <caf\xe9-1@example.com>\r\n"
            b"Subject: Receipt\r\n"
            b"From: shop@example.com\r\n\r\nbody\r\n"
        )
        fake = FakeIMAPClient({1: msg})
        adapter = ImapAdapter(imap_config)
        adapter._client = fake  # type: ignore[assignment]

        (ref,) = adapter.list_new(None).items

        assert ref.source_id.startswith("<caf")
        assert ref.source_id.endswith("-1@example.com>")
        assert ref.source_id == ImapAdapter._get_message_id(message_from_bytes(msg))
