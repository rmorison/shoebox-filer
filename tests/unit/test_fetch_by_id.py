"""``fetch_by_id``: re-fetching one item by its source ID, for re-extraction.

IMAP searches by ``Message-ID`` and returns the message only when exactly one
matches; Drive gets the file by ID. Errors mean what they mean for ``fetch``:
``SourceUnavailableError`` for the source, ``None`` for an item that is gone.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.adapters.gdrive import GdriveAdapter
from receipt_index.adapters.imap import ImapAdapter, MessageRefusedError
from tests.unit.test_gdrive_adapter import (
    FakeDrive,
    _http_error,
    _make_gdrive_config,
)
from tests.unit.test_imap_adapter import FakeIMAPClient, _make_simple_email

if TYPE_CHECKING:
    from receipt_index.config import ImapSourceConfig

# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

_SEARCH = re.compile(r'^HEADER Message-ID "((?:[^"\\]|\\.)*)"$')


class SearchingIMAPClient(FakeIMAPClient):
    """The fake server, plus ``UID SEARCH HEADER Message-ID "<quoted>"``.

    Like a real server, it unquotes the string and matches it as a substring
    of the header (RFC 3501 6.4.4), so the adapter must check the match.
    """

    def __init__(self, messages: dict[int, bytes]) -> None:
        super().__init__(messages)
        self.raw_searches: list[Any] = []
        self.search_error: Exception | None = None
        # Raised by the next header FETCH (the exact-ID check after SEARCH).
        self.header_error: Exception | None = None

    def fetch(self, uids: list[int], items: list[str]) -> dict[int, dict[bytes, Any]]:
        if items != ["BODY.PEEK[]"] and self.header_error is not None:
            raise self.header_error
        return super().fetch(uids, items)

    def search(self, criteria: Any) -> list[int]:
        self.raw_searches.append(criteria)
        if self.search_error is not None:
            raise self.search_error
        if not isinstance(criteria, str):
            return super().search(criteria)
        match = _SEARCH.match(criteria)
        assert match is not None, f"unexpected search: {criteria!r}"
        wanted = re.sub(r"\\(.)", r"\1", match.group(1))
        found = []
        for uid, raw in self.messages.items():
            head = raw.split(b"\n\n", 1)[0].decode()
            ids = re.findall(r"(?im)^Message-ID:\s*(.*)$", head)
            if any(wanted in i.strip() for i in ids):
                found.append(uid)
        return sorted(found)


def _imap(config: ImapSourceConfig, fake: SearchingIMAPClient) -> ImapAdapter:
    adapter = ImapAdapter(config)
    adapter._client = fake  # type: ignore[assignment]
    return adapter


def _mail(message_id: str | None, subject: str = "Receipt") -> bytes:
    return _make_simple_email(message_id=message_id, subject=subject)


class TestImapFetchById:
    def test_one_match_fetches_the_body(self, imap_config: ImapSourceConfig) -> None:
        fake = SearchingIMAPClient(
            {3: _mail("<a@example.com>", "A"), 4: _mail("<b@example.com>", "B")}
        )
        raw = _imap(imap_config, fake).fetch_by_id("<b@example.com>")
        assert raw is not None
        assert raw.subject == "B"
        assert raw.source_id == "<b@example.com>"
        assert fake.body_fetches() == [4]
        assert fake.selected == ("INBOX", True)  # EXAMINE: read-only

    def test_no_match_is_none(self, imap_config: ImapSourceConfig) -> None:
        fake = SearchingIMAPClient({3: _mail("<a@example.com>")})
        assert _imap(imap_config, fake).fetch_by_id("<gone@example.com>") is None
        assert fake.body_fetches() == []

    def test_two_matches_are_none(self, imap_config: ImapSourceConfig) -> None:
        # A Message-ID is set by the sender and isn't guaranteed unique.
        fake = SearchingIMAPClient(
            {3: _mail("<dup@example.com>", "A"), 4: _mail("<dup@example.com>", "B")}
        )
        assert _imap(imap_config, fake).fetch_by_id("<dup@example.com>") is None
        assert fake.body_fetches() == []

    def test_substring_match_is_not_a_match(
        self, imap_config: ImapSourceConfig
    ) -> None:
        # SEARCH HEADER matches substrings, so a search for <a@example.com>
        # also finds a header that contains it. Only the exact ID counts.
        fake = SearchingIMAPClient({3: _mail("<a@example.com>.resent")})
        assert _imap(imap_config, fake).fetch_by_id("<a@example.com>") is None

    @pytest.mark.parametrize(
        "message_id",
        [
            '<quo"te@example.com>',
            "<back\\slash@example.com>",
            "<paren(1)@example.com>",
            "<sp ace@example.com>",
            "<star*%{1}@example.com>",
        ],
    )
    def test_awkward_ids_are_quoted(
        self, imap_config: ImapSourceConfig, message_id: str
    ) -> None:
        fake = SearchingIMAPClient({7: _mail(message_id)})
        raw = _imap(imap_config, fake).fetch_by_id(message_id)
        assert raw is not None
        # Sent as one string, which IMAPClient passes on without quoting it again.
        (criteria,) = fake.raw_searches
        assert isinstance(criteria, str)

    @pytest.mark.parametrize(
        "source_id",
        [
            hashlib.sha256(b"subject|date|sender").hexdigest(),  # hash fallback
            "imap-uid:test-imap:INBOX:7:12",  # headers refused at listing
            "<line\r\nbreak@example.com>",  # can't be an IMAP quoted string
            "<café@example.com>",  # not ASCII
        ],
    )
    def test_unsearchable_ids_are_none_without_a_search(
        self, imap_config: ImapSourceConfig, source_id: str
    ) -> None:
        fake = SearchingIMAPClient({7: _mail("<a@example.com>")})
        assert _imap(imap_config, fake).fetch_by_id(source_id) is None
        assert fake.raw_searches == []

    def test_dropped_connection_is_unavailable(
        self, imap_config: ImapSourceConfig
    ) -> None:
        fake = SearchingIMAPClient({7: _mail("<a@example.com>")})
        fake.search_error = IMAPClientAbortError("socket closed")
        with pytest.raises(SourceUnavailableError):
            _imap(imap_config, fake).fetch_by_id("<a@example.com>")

    @pytest.mark.parametrize(
        "error",
        [IMAPClientAbortError("socket closed"), OSError("connection reset")],
        ids=["abort", "oserror"],
    )
    def test_drop_after_search_is_unavailable(
        self, imap_config: ImapSourceConfig, error: Exception
    ) -> None:
        # The connection drops during the exact-ID header check, after SEARCH
        # succeeded: still an outage, not this message's fault.
        fake = SearchingIMAPClient({7: _mail("<a@example.com>")})
        fake.header_error = error
        adapter = _imap(imap_config, fake)
        with pytest.raises(SourceUnavailableError):
            adapter.fetch_by_id("<a@example.com>")
        assert adapter._client is None  # the broken session was dropped

    def test_connect_failure_is_unavailable(
        self, imap_config: ImapSourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = ImapAdapter(imap_config)

        def refuse() -> None:
            raise ConnectionError("Failed to connect to IMAP after 3 attempts")

        monkeypatch.setattr(adapter, "_connect", refuse)
        with pytest.raises(SourceUnavailableError):
            adapter.fetch_by_id("<a@example.com>")

    def test_session_refusal_is_unavailable(
        self, imap_config: ImapSourceConfig
    ) -> None:
        fake = SearchingIMAPClient({7: _mail("<a@example.com>")})
        fake.search_error = IMAPClientError("SEARCH failed: NO [UNAVAILABLE] later")
        with pytest.raises(SourceUnavailableError):
            _imap(imap_config, fake).fetch_by_id("<a@example.com>")

    def test_refused_search_is_an_item_error(
        self, imap_config: ImapSourceConfig
    ) -> None:
        # A BAD for this one search (say the server rejects the criteria) is
        # the item's problem: re-extraction falls back to the rendition.
        fake = SearchingIMAPClient({7: _mail("<a@example.com>")})
        fake.search_error = IMAPClientError("SEARCH command error: BAD [parse]")
        with pytest.raises(MessageRefusedError):
            _imap(imap_config, fake).fetch_by_id("<a@example.com>")


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------


class GettingDrive(FakeDrive):
    """The fake Drive service, plus ``files.get(fileId=..., fields=...)``."""

    def __init__(self, metas: dict[str, dict[str, Any]], **kw: Any) -> None:
        super().__init__(**kw)
        self.metas = metas
        self.get_error: Exception | None = None
        self.gets: list[str] = []

    def get(self, fields: str, fileId: str | None = None) -> Any:  # noqa: N803
        if fileId is None:  # about.get
            return super().get(fields)
        self.gets.append(fileId)

        def run() -> dict[str, Any]:
            if self.get_error is not None:
                raise self.get_error
            if fileId not in self.metas:
                raise _http_error(404, b'{"error": {"code": 404}}')
            return self.metas[fileId]

        return type("Req", (), {"execute": staticmethod(run)})()


def _drive(service: GettingDrive) -> GdriveAdapter:
    adapter = GdriveAdapter(_make_gdrive_config())
    adapter._service = service
    return adapter


def _meta(file_id: str, mime: str = "application/pdf") -> dict[str, Any]:
    return {
        "id": file_id,
        "name": f"{file_id}.pdf",
        "mimeType": mime,
        "modifiedTime": "2026-01-15T10:00:00.000Z",
    }


class TestDriveFetchById:
    def test_found_is_downloaded(self) -> None:
        service = GettingDrive({"f1": _meta("f1")})
        raw = _drive(service).fetch_by_id("f1")
        assert raw is not None
        assert raw.source_id == "f1"
        assert raw.file_name == "f1.pdf"
        assert raw.file_content == b"%PDF-f1"
        assert raw.file_content_type == "application/pdf"

    def test_not_found_is_none(self) -> None:
        assert _drive(GettingDrive({})).fetch_by_id("gone") is None

    def test_unsupported_type_is_none(self) -> None:
        service = GettingDrive({"z": _meta("z", "application/zip")})
        assert _drive(service).fetch_by_id("z") is None

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_service_error_is_unavailable(self, status: int) -> None:
        service = GettingDrive({"f1": _meta("f1")})
        service.get_error = _http_error(status)
        with pytest.raises(SourceUnavailableError):
            _drive(service).fetch_by_id("f1")

    def test_file_level_403_is_an_item_error(self) -> None:
        service = GettingDrive({"f1": _meta("f1")})
        service.get_error = _http_error(403, b'{"reason": "forbidden"}')
        with pytest.raises(Exception) as caught:
            _drive(service).fetch_by_id("f1")
        assert not isinstance(caught.value, SourceUnavailableError)


class TestDriveSetupFailures:
    @pytest.mark.parametrize(
        "error",
        [ValueError("bad token_json"), KeyError("refresh_token")],
        ids=["value", "key"],
    )
    def test_a_service_that_cant_be_built_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        adapter = GdriveAdapter(_make_gdrive_config())

        def broken() -> Any:
            raise error

        monkeypatch.setattr(adapter, "_build_service", broken)
        with pytest.raises(SourceUnavailableError, match="Drive"):
            adapter.fetch_by_id("f1")


class TestRefusedExactIdCheck:
    def test_refused_header_check_is_an_item_error_not_a_miss(
        self, imap_config: ImapSourceConfig
    ) -> None:
        # SEARCH found the message, but the server refuses its headers (a
        # corrupt message file). That isn't "no such message": backfill
        # would otherwise mark a present original unavailable for good.
        fake = SearchingIMAPClient({7: _mail("<a@example.com>")})
        fake.header_error = IMAPClientError(
            "FETCH command error: NO [SERVERBUG] Internal error"
        )
        with pytest.raises(MessageRefusedError, match="UID 7"):
            _imap(imap_config, fake).fetch_by_id("<a@example.com>")
        assert fake.body_fetches() == []
