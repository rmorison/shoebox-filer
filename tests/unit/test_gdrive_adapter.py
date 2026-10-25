"""Tests for receipt_index.adapters.gdrive."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from receipt_index.adapters.gdrive import GdriveAdapter, _parse_drive_date
from receipt_index.config import GdriveSourceConfig


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


class TestFetchUnprocessed:
    """Tests for the full fetch_unprocessed flow."""

    def test_yields_unprocessed_files(self) -> None:
        config = _make_gdrive_config(name="my-drive")
        files = [
            _make_file_meta(file_id="f1", name="receipt1.pdf"),
            _make_file_meta(
                file_id="f2",
                name="receipt2.jpg",
                mime_type="image/jpeg",
                modified_time="2024-03-20T15:00:00.000Z",
            ),
        ]
        pdf_content = b"%PDF-1.4"
        service = _mock_drive_service(files=files, download_content=pdf_content)

        adapter = GdriveAdapter(config)
        adapter._build_service = MagicMock(return_value=service)  # type: ignore[method-assign]
        results = list(adapter.fetch_unprocessed(set()))

        assert len(results) == 2
        assert results[0].source_id == "f1"
        assert results[0].source_name == "my-drive"
        assert results[0].source_type == "gdrive"
        assert results[0].file_name == "receipt1.pdf"
        assert results[0].file_content == pdf_content
        assert results[0].file_content_type == "application/pdf"

        assert results[1].source_id == "f2"
        assert results[1].date == datetime(2024, 3, 20, 15, 0, 0, tzinfo=UTC)

    def test_skips_already_processed_files(self) -> None:
        config = _make_gdrive_config()
        files = [
            _make_file_meta(file_id="f1"),
            _make_file_meta(file_id="f2"),
            _make_file_meta(file_id="f3"),
        ]
        service = _mock_drive_service(files=files, download_content=b"data")

        adapter = GdriveAdapter(config)
        adapter._build_service = MagicMock(return_value=service)  # type: ignore[method-assign]
        results = list(adapter.fetch_unprocessed({"f1", "f3"}))

        assert len(results) == 1
        assert results[0].source_id == "f2"

    def test_skips_unsupported_files_in_flow(self) -> None:
        config = _make_gdrive_config()
        files = [
            _make_file_meta(file_id="f1", mime_type="application/pdf"),
            _make_file_meta(file_id="f2", mime_type="application/zip", name="data.zip"),
        ]
        service = _mock_drive_service(files=files, download_content=b"pdf-data")

        adapter = GdriveAdapter(config)
        adapter._build_service = MagicMock(return_value=service)  # type: ignore[method-assign]
        results = list(adapter.fetch_unprocessed(set()))

        assert len(results) == 1
        assert results[0].source_id == "f1"

    def test_empty_folder_yields_nothing(self) -> None:
        config = _make_gdrive_config()
        service = _mock_drive_service(files=[])

        adapter = GdriveAdapter(config)
        adapter._build_service = MagicMock(return_value=service)  # type: ignore[method-assign]
        results = list(adapter.fetch_unprocessed(set()))

        assert results == []

    def test_conforms_to_source_adapter_protocol(self) -> None:
        from receipt_index.adapters.base import SourceAdapter

        config = _make_gdrive_config()
        adapter = GdriveAdapter(config)
        assert isinstance(adapter, SourceAdapter)
