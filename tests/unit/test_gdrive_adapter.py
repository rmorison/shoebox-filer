"""Tests for receipt_index.adapters.gdrive."""

from __future__ import annotations

import http.client
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from google.auth import exceptions as google_auth_exceptions
from googleapiclient.errors import HttpError

from receipt_index.adapters.base import SourceAdapter, SourceUnavailableError
from receipt_index.adapters.gdrive import (
    UNSUPPORTED_REASON,
    GdriveAdapter,
    _is_invalid_token_error,
    _parse_drive_date,
)
from receipt_index.config import GdriveSourceConfig
from receipt_index.models import SyncState


def _make_gdrive_config(
    *,
    name: str = "test-drive",
    folder_id: str = "folder_abc123",
) -> GdriveSourceConfig:
    """Build a GdriveSourceConfig with fake credential JSON."""
    token_json = json.dumps(
        {
            "token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",  # pragma: allowlist secret
        }
    )
    return GdriveSourceConfig(
        name=name,
        folder_id=folder_id,
        token_json=token_json,
    )


def _make_file_meta(
    *,
    file_id: str = "file_1",
    name: str = "receipt.pdf",
    mime_type: str = "application/pdf",
    modified_time: str = "2024-01-15T10:30:00.000Z",
) -> dict[str, Any]:
    """Build a Drive API file metadata dict."""
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "modifiedTime": modified_time,
    }


def _mock_drive_service(
    files: list[dict[str, Any]] | None = None,
    *,
    pages: list[list[dict[str, Any]]] | None = None,
    download_content: bytes = b"file-content",
) -> MagicMock:
    """Create a mock Google Drive service.

    Args:
        files: Single page of file metadata (convenience for single-page tests).
        pages: Multiple pages of file metadata for pagination tests.
        download_content: Bytes returned by get_media/export_media.
    """
    service = MagicMock()

    if pages is not None:
        # Multi-page responses
        responses = []
        for i, page_files in enumerate(pages):
            resp: dict[str, Any] = {"files": page_files}
            if i < len(pages) - 1:
                resp["nextPageToken"] = f"token_{i + 1}"
            responses.append(resp)
        service.files().list().execute.side_effect = responses
        # Chain the list mock so each call returns a new mock with execute
        list_mocks = []
        for resp in responses:
            m = MagicMock()
            m.execute.return_value = resp
            list_mocks.append(m)
        service.files().list.side_effect = list_mocks
    else:
        # Single-page response
        response: dict[str, Any] = {"files": files or []}
        list_mock = MagicMock()
        list_mock.execute.return_value = response
        service.files().list.return_value = list_mock

    # Download mocks
    get_media_mock = MagicMock()
    get_media_mock.execute.return_value = download_content
    service.files().get_media.return_value = get_media_mock

    export_media_mock = MagicMock()
    export_media_mock.execute.return_value = download_content
    service.files().export_media.return_value = export_media_mock

    return service


class TestParseDriveDate:
    """Tests for _parse_drive_date."""

    def test_parses_standard_drive_timestamp(self) -> None:
        result = _parse_drive_date("2024-01-15T10:30:00.000Z")
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_parses_without_fractional_seconds(self) -> None:
        result = _parse_drive_date("2024-06-01T00:00:00Z")
        assert result == datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)

    def test_parses_with_microseconds(self) -> None:
        result = _parse_drive_date("2024-12-31T23:59:59.123456Z")
        expected = datetime(2024, 12, 31, 23, 59, 59, 123456, tzinfo=UTC)
        assert result == expected

    def test_result_is_utc(self) -> None:
        result = _parse_drive_date("2024-01-15T10:30:00.000Z")
        assert result.tzinfo is UTC


class TestBuildService:
    """Tests for _build_service credential building."""

    @patch("google.oauth2.credentials.Credentials.from_authorized_user_info")
    @patch("googleapiclient.discovery.build")
    def test_builds_service_from_token_json(
        self,
        mock_build: MagicMock,
        mock_from_user_info: MagicMock,
    ) -> None:
        config = _make_gdrive_config()
        creds = MagicMock()
        creds.expired = False
        mock_from_user_info.return_value = creds

        expected_service = MagicMock()
        mock_build.return_value = expected_service

        adapter = GdriveAdapter(config)
        service = adapter._build_service()

        token_data = json.loads(config.token_json)
        mock_from_user_info.assert_called_once_with(token_data)
        mock_build.assert_called_once_with("drive", "v3", credentials=creds)
        assert service is expected_service

    @patch("googleapiclient.discovery.build")
    @patch("google.auth.transport.requests.Request")
    @patch("google.oauth2.credentials.Credentials.from_authorized_user_info")
    def test_refreshes_expired_credentials(
        self,
        mock_from_user_info: MagicMock,
        mock_request_cls: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        config = _make_gdrive_config()
        creds = MagicMock()
        creds.expired = True
        creds.refresh_token = "fake-refresh"
        mock_from_user_info.return_value = creds

        adapter = GdriveAdapter(config)
        adapter._build_service()

        creds.refresh.assert_called_once_with(mock_request_cls())

    @patch("googleapiclient.discovery.build")
    @patch("google.oauth2.credentials.Credentials.from_authorized_user_info")
    def test_does_not_refresh_when_not_expired(
        self,
        mock_from_user_info: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        config = _make_gdrive_config()
        creds = MagicMock()
        creds.expired = False
        mock_from_user_info.return_value = creds

        adapter = GdriveAdapter(config)
        adapter._build_service()

        creds.refresh.assert_not_called()


class TestListFiles:
    """Tests for _list_files."""

    def test_lists_files_single_page(self) -> None:
        config = _make_gdrive_config(folder_id="folder_xyz")
        files = [
            _make_file_meta(file_id="f1", name="receipt1.pdf"),
            _make_file_meta(file_id="f2", name="receipt2.jpg"),
        ]
        service = _mock_drive_service(files=files)

        adapter = GdriveAdapter(config)
        result = adapter._list_files(service)

        assert len(result) == 2
        assert result[0]["id"] == "f1"
        assert result[1]["id"] == "f2"

        # Verify query includes folder_id
        call_kwargs = service.files().list.call_args
        assert "folder_xyz" in call_kwargs.kwargs.get("q", call_kwargs[1].get("q", ""))

    def test_handles_pagination(self) -> None:
        config = _make_gdrive_config()
        page1 = [_make_file_meta(file_id="f1")]
        page2 = [_make_file_meta(file_id="f2")]
        service = _mock_drive_service(pages=[page1, page2])

        adapter = GdriveAdapter(config)
        result = adapter._list_files(service)

        assert len(result) == 2
        assert result[0]["id"] == "f1"
        assert result[1]["id"] == "f2"

    def test_empty_folder(self) -> None:
        config = _make_gdrive_config()
        service = _mock_drive_service(files=[])

        adapter = GdriveAdapter(config)
        result = adapter._list_files(service)

        assert result == []


class TestDownloadFile:
    """Tests for _download_file."""

    def test_downloads_pdf(self) -> None:
        config = _make_gdrive_config()
        pdf_content = b"%PDF-1.4 content"
        service = _mock_drive_service(download_content=pdf_content)
        file_meta = _make_file_meta(mime_type="application/pdf")

        adapter = GdriveAdapter(config)
        result = adapter._download_file(service, file_meta)

        assert result is not None
        content, mime_type = result
        assert content == pdf_content
        assert mime_type == "application/pdf"
        service.files().get_media.assert_called_once_with(fileId="file_1")

    def test_downloads_jpeg(self) -> None:
        config = _make_gdrive_config()
        img_content = b"\xff\xd8\xff\xe0 jpeg data"
        service = _mock_drive_service(download_content=img_content)
        file_meta = _make_file_meta(mime_type="image/jpeg", name="scan.jpg")

        adapter = GdriveAdapter(config)
        result = adapter._download_file(service, file_meta)

        assert result is not None
        content, mime_type = result
        assert content == img_content
        assert mime_type == "image/jpeg"

    def test_downloads_png(self) -> None:
        config = _make_gdrive_config()
        img_content = b"\x89PNG\r\n\x1a\n"
        service = _mock_drive_service(download_content=img_content)
        file_meta = _make_file_meta(mime_type="image/png", name="scan.png")

        adapter = GdriveAdapter(config)
        result = adapter._download_file(service, file_meta)

        assert result is not None
        content, mime_type = result
        assert content == img_content
        assert mime_type == "image/png"

    def test_exports_google_doc_as_pdf(self) -> None:
        config = _make_gdrive_config()
        pdf_content = b"%PDF-1.4 exported"
        service = _mock_drive_service(download_content=pdf_content)
        file_meta = _make_file_meta(
            mime_type="application/vnd.google-apps.document",
            name="My Document",
        )

        adapter = GdriveAdapter(config)
        result = adapter._download_file(service, file_meta)

        assert result is not None
        content, mime_type = result
        assert content == pdf_content
        assert mime_type == "application/pdf"
        service.files().export_media.assert_called_once_with(
            fileId="file_1", mimeType="application/pdf"
        )

    def test_skips_unsupported_mime_type(self) -> None:
        config = _make_gdrive_config()
        service = _mock_drive_service()
        file_meta = _make_file_meta(mime_type="application/zip", name="archive.zip")

        adapter = GdriveAdapter(config)
        result = adapter._download_file(service, file_meta)

        assert result is None


# ---------------------------------------------------------------------------
# Changes feed
# ---------------------------------------------------------------------------

FOLDER = "folder_abc123"
OLD = "2024-01-15T10:30:00.000Z"

# Drive v3's documented answer to a bad or expired pageToken. Recording the
# live response is deferred to the evidence store's Drive ticket.
INVALID_TOKEN_BODY = json.dumps(
    {
        "error": {
            "code": 400,
            "message": "Invalid Value",
            "errors": [
                {
                    "message": "Invalid Value",
                    "domain": "global",
                    "reason": "invalid",
                    "location": "pageToken",
                    "locationType": "parameter",
                }
            ],
        }
    }
).encode()


def _http_error(status: int, body: bytes = b"{}") -> HttpError:
    return HttpError(httplib2.Response({"status": status}), body)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _Req:
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def execute(self) -> Any:
        return self._fn()


class FakeDrive:
    """A Drive v3 service for the adapter: about, changes and files."""

    def __init__(
        self,
        *,
        account: str = "acct-1",
        files: list[dict[str, Any]] | None = None,
        pages: dict[str, dict[str, Any]] | None = None,
        start_token: str = "start-1",
        list_error: HttpError | None = None,
    ) -> None:
        self.account = account
        self.files_in_folder = files or []
        self.pages = pages or {}
        self.start_token = start_token
        self.list_error = list_error
        self.calls: list[str] = []

    def about(self) -> FakeDrive:
        return self

    def get(self, fields: str) -> _Req:
        return _Req(lambda: {"user": {"permissionId": self.account}})

    def changes(self) -> FakeDrive:
        return self

    def getStartPageToken(self) -> _Req:  # noqa: N802 (Drive API name)
        def run() -> dict[str, Any]:
            self.calls.append("getStartPageToken")
            return {"startPageToken": self.start_token}

        return _Req(run)

    def list(self, **kwargs: Any) -> _Req:
        if "pageToken" in kwargs and "spaces" in kwargs:  # changes.list

            def run_changes() -> dict[str, Any]:
                self.calls.append(f"changes.list:{kwargs['pageToken']}")
                if self.list_error is not None:
                    raise self.list_error
                assert kwargs["spaces"] == "drive"
                return self.pages[kwargs["pageToken"]]

            return _Req(run_changes)

        def run_files() -> dict[str, Any]:  # files.list
            self.calls.append("files.list")
            assert f"'{FOLDER}' in parents" in kwargs["q"]
            return {"files": self.files_in_folder}

        return _Req(run_files)

    def files(self) -> FakeDrive:
        return self

    def get_media(self, fileId: str) -> _Req:  # noqa: N803
        return _Req(lambda: b"%PDF-" + fileId.encode())

    def export_media(self, fileId: str, mimeType: str) -> _Req:  # noqa: N803
        return _Req(lambda: b"%PDF-export-" + fileId.encode())


def _drive_state(token: str | None, account: str | None) -> SyncState:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SyncState.model_validate(
        {
            "id": "019572a0-0000-7000-8000-000000000002",
            "source_name": "test-drive",
            "source_type": "gdrive",
            "folder": FOLDER,
            "drive_page_token": token,
            "drive_account_id": account,
            "created_at": now,
            "updated_at": now,
        }
    )


def _change(
    file_id: str,
    *,
    parents: list[str] | None = None,
    mime_type: str = "application/pdf",
    trashed: bool = False,
    removed: bool = False,
    time: str = OLD,
    name: str | None = None,
) -> dict[str, Any]:
    change: dict[str, Any] = {"fileId": file_id, "removed": removed, "time": time}
    if not removed:
        change["file"] = {
            "id": file_id,
            "name": name or f"{file_id}.pdf",
            "mimeType": mime_type,
            "parents": [FOLDER] if parents is None else parents,
            "trashed": trashed,
            "modifiedTime": OLD,
        }
    return change


def _adapter(drive: FakeDrive, settle: int = 60) -> GdriveAdapter:
    adapter = GdriveAdapter(_make_gdrive_config(), settle_seconds=settle)
    adapter._service = drive
    return adapter


class TestBootstrap:
    def test_takes_the_start_token_before_listing(self) -> None:
        drive = FakeDrive(files=[_make_file_meta(file_id="f1")])
        read = _adapter(drive).list_new(None)

        assert drive.calls == ["getStartPageToken", "files.list"]
        assert [r.source_id for r in read.items] == ["f1"]
        assert read.drive_page_token == "start-1"
        assert read.drive_account_id == "acct-1"
        assert read.reset is None
        assert read.held_back is False

    def test_token_without_account_is_a_first_run(self) -> None:
        drive = FakeDrive()
        read = _adapter(drive).list_new(_drive_state("tok-9", None))
        assert drive.calls == ["getStartPageToken", "files.list"]
        assert read.reset is None

    def test_row_without_token_is_a_first_run(self) -> None:
        drive = FakeDrive()
        read = _adapter(drive).list_new(_drive_state(None, None))
        assert drive.calls == ["getStartPageToken", "files.list"]
        assert read.reset is None

    def test_drops_subfolders(self) -> None:
        drive = FakeDrive(
            files=[
                _make_file_meta(file_id="f1"),
                _make_file_meta(
                    file_id="needs-attention",
                    name="needs-attention",
                    mime_type="application/vnd.google-apps.folder",
                ),
            ]
        )
        read = _adapter(drive).list_new(None)
        assert [r.source_id for r in read.items] == ["f1"]

    def test_settles_on_modified_time(self) -> None:
        fresh = _iso(datetime.now(tz=UTC) - timedelta(seconds=5))
        drive = FakeDrive(
            files=[
                _make_file_meta(file_id="old"),
                _make_file_meta(file_id="new", modified_time=fresh),
            ]
        )
        read = _adapter(drive).list_new(None)
        assert [r.source_id for r in read.items] == ["old"]
        assert read.held_back is True

    def test_unsupported_type_becomes_a_skip_ref(self) -> None:
        drive = FakeDrive(
            files=[_make_file_meta(file_id="z", mime_type="application/zip")]
        )
        (ref,) = _adapter(drive).list_new(None).items
        assert ref.skip_reason == UNSUPPORTED_REASON

    def test_changed_account_rebootstraps(self) -> None:
        drive = FakeDrive(account="acct-2")
        read = _adapter(drive).list_new(_drive_state("tok-9", "acct-1"))
        assert drive.calls == ["getStartPageToken", "files.list"]
        assert read.reset == "account changed"
        assert read.drive_account_id == "acct-2"


class TestPoll:
    def test_reads_from_the_stored_token_and_filters(self) -> None:
        drive = FakeDrive(
            pages={
                "tok-1": {
                    "nextPageToken": "tok-2",
                    "changes": [
                        _change("keep-1"),
                        _change("elsewhere", parents=["other-folder"]),
                        _change("binned", trashed=True),
                        _change(
                            "sub",
                            mime_type="application/vnd.google-apps.folder",
                        ),
                    ],
                },
                "tok-2": {
                    "newStartPageToken": "tok-3",
                    "changes": [_change("gone", removed=True), _change("keep-2")],
                },
            }
        )
        read = _adapter(drive).list_new(_drive_state("tok-1", "acct-1"))

        assert drive.calls == ["changes.list:tok-1", "changes.list:tok-2"]
        assert [r.source_id for r in read.items] == ["keep-1", "keep-2"]
        assert read.drive_page_token == "tok-3"
        assert read.held_back is False
        assert read.reset is None

    def test_latest_change_for_a_file_wins(self) -> None:
        drive = FakeDrive(
            pages={
                "tok-1": {
                    "newStartPageToken": "tok-2",
                    "changes": [_change("f1"), _change("f1", trashed=True)],
                }
            }
        )
        read = _adapter(drive).list_new(_drive_state("tok-1", "acct-1"))
        assert read.items == []

    def test_settles_on_the_change_time_not_modified_time(self) -> None:
        fresh = _iso(datetime.now(tz=UTC) - timedelta(seconds=10))
        drive = FakeDrive(
            pages={
                "tok-1": {
                    "newStartPageToken": "tok-2",
                    # The file claims an old modifiedTime; the change is fresh.
                    "changes": [_change("young", time=fresh), _change("old")],
                }
            }
        )
        read = _adapter(drive).list_new(_drive_state("tok-1", "acct-1"))
        assert [r.source_id for r in read.items] == ["old"]
        assert read.held_back is True

    def test_zero_settle_takes_everything(self) -> None:
        fresh = _iso(datetime.now(tz=UTC))
        drive = FakeDrive(
            pages={
                "tok-1": {
                    "newStartPageToken": "tok-2",
                    "changes": [_change("young", time=fresh)],
                }
            }
        )
        read = _adapter(drive, settle=0).list_new(_drive_state("tok-1", "acct-1"))
        assert [r.source_id for r in read.items] == ["young"]
        assert read.held_back is False

    def test_unsupported_type_is_kept_as_a_skip_ref(self) -> None:
        drive = FakeDrive(
            pages={
                "tok-1": {
                    "newStartPageToken": "tok-2",
                    "changes": [_change("doc.zip", mime_type="application/zip")],
                }
            }
        )
        (ref,) = _adapter(drive).list_new(_drive_state("tok-1", "acct-1")).items
        assert ref.skip_reason == UNSUPPORTED_REASON

    @pytest.mark.parametrize(
        "error",
        [
            _http_error(400, INVALID_TOKEN_BODY),
            _http_error(404),
            _http_error(410),
        ],
        ids=["400-pageToken", "404", "410"],
    )
    def test_invalid_token_rebootstraps(self, error: HttpError) -> None:
        drive = FakeDrive(files=[_make_file_meta(file_id="f1")], list_error=error)
        read = _adapter(drive).list_new(_drive_state("stale", "acct-1"))

        assert drive.calls == ["changes.list:stale", "getStartPageToken", "files.list"]
        assert read.reset == "invalid page token"
        assert read.drive_page_token == "start-1"
        assert [r.source_id for r in read.items] == ["f1"]

    def test_a_repeating_page_token_fails_instead_of_looping(self) -> None:
        drive = FakeDrive(pages={"tok-1": {"nextPageToken": "tok-1", "changes": []}})
        with pytest.raises(RuntimeError, match="bad page token"):
            _adapter(drive).list_new(_drive_state("tok-1", "acct-1"))

    def test_other_errors_propagate(self) -> None:
        error = _http_error(400, b'{"error": {"message": "Bad fields"}}')
        drive = FakeDrive(list_error=error)
        with pytest.raises(HttpError):
            _adapter(drive).list_new(_drive_state("tok-1", "acct-1"))
        assert _is_invalid_token_error(_http_error(500)) is False
        assert _is_invalid_token_error(ValueError("x")) is False


class TestFetchFile:
    def test_downloads_the_ref(self) -> None:
        drive = FakeDrive(files=[_make_file_meta(file_id="f1", name="r.pdf")])
        adapter = _adapter(drive)
        (ref,) = adapter.list_new(None).items
        raw = adapter.fetch(ref)

        assert raw is not None
        assert raw.source_id == "f1"
        assert raw.source_name == "test-drive"
        assert raw.source_type == "gdrive"
        assert raw.file_name == "r.pdf"
        assert raw.file_content == b"%PDF-f1"
        assert raw.file_content_type == "application/pdf"
        assert raw.date == datetime(2024, 1, 15, 10, 30, tzinfo=UTC)

    def test_exports_google_doc(self) -> None:
        drive = FakeDrive(
            files=[
                _make_file_meta(
                    file_id="d1", mime_type="application/vnd.google-apps.document"
                )
            ]
        )
        adapter = _adapter(drive)
        (ref,) = adapter.list_new(None).items
        raw = adapter.fetch(ref)
        assert raw is not None
        assert raw.file_content == b"%PDF-export-d1"

    def test_conforms_to_source_adapter_protocol(self) -> None:
        assert isinstance(GdriveAdapter(_make_gdrive_config()), SourceAdapter)


class TestDownloadErrors:
    def _failing(self, error: Exception) -> tuple[GdriveAdapter, Any]:
        drive = FakeDrive(files=[_make_file_meta(file_id="f1")])
        adapter = _adapter(drive)
        (ref,) = adapter.list_new(None).items

        def fail(*_: Any, **__: Any) -> Any:
            raise error

        drive.get_media = fail  # type: ignore[method-assign]
        return adapter, ref

    @pytest.mark.parametrize(
        "error",
        [
            _http_error(503),
            _http_error(500),
            _http_error(429),
            _http_error(
                403,
                b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}',
            ),
            TimeoutError("timed out"),
            http.client.IncompleteRead(b"partial"),
            httplib2.ServerNotFoundError("Unable to find the server"),
            google_auth_exceptions.TransportError("refresh: connection refused"),
            google_auth_exceptions.RefreshError("invalid_grant"),
        ],
        ids=[
            "503",
            "500",
            "429",
            "403-rate-limit",
            "timeout",
            "cut-off-body",
            "dns",
            "refresh-transport",
            "refresh-refused",
        ],
    )
    def test_service_errors_are_a_source_outage(self, error: Exception) -> None:
        adapter, ref = self._failing(error)
        with pytest.raises(SourceUnavailableError):
            adapter.fetch(ref)

    @pytest.mark.parametrize(
        "error",
        [_http_error(404), _http_error(403, b'{"error": {"message": "Forbidden"}}')],
        ids=["404", "403-forbidden"],
    )
    def test_file_errors_stay_item_errors(self, error: HttpError) -> None:
        adapter, ref = self._failing(error)
        with pytest.raises(HttpError):
            adapter.fetch(ref)
