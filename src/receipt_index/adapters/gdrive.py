"""Google Drive source adapter."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from receipt_index.models import RawReceipt

if TYPE_CHECKING:
    from collections.abc import Iterator

    from receipt_index.config import GdriveSourceConfig

logger = logging.getLogger(__name__)

_SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
}

_GOOGLE_DOCS_MIME_TYPE = "application/vnd.google-apps.document"

_FILE_FIELDS = "nextPageToken, files(id, name, mimeType, modifiedTime)"


def _parse_drive_date(date_str: str) -> datetime:
    """Parse an RFC 3339 timestamp from the Google Drive API.

    Google Drive returns timestamps like '2024-01-15T10:30:00.000Z'.
    """
    # Replace Z suffix with +00:00 for fromisoformat compatibility
    cleaned = date_str.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned).astimezone(UTC)


class GdriveAdapter:
    """Fetch unprocessed receipt files from a Google Drive folder."""

    def __init__(self, config: GdriveSourceConfig) -> None:
        self.config = config

    def fetch_unprocessed(self, processed_ids: set[str]) -> Iterator[RawReceipt]:
        """List files in the configured Drive folder and yield unprocessed ones."""
        service = self._build_service()
        files = self._list_files(service)

        for file_meta in files:
            file_id: str = file_meta["id"]
            if file_id in processed_ids:
                logger.debug("Skipping already-processed file %s", file_id)
                continue

            result = self._download_file(service, file_meta)
            if result is None:
                continue

            content, mime_type = result
            yield RawReceipt(
                source_id=file_id,
                source_name=self.config.name,
                source_type="gdrive",
                date=_parse_drive_date(file_meta["modifiedTime"]),
                file_name=file_meta["name"],
                file_content=content,
                file_content_type=mime_type,
            )

    def _build_service(self) -> Any:
        """Create a Google Drive API service from stored credentials.

        Uses lazy imports for Google API libraries.
        """
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_data = json.loads(self.config.token_json)
        creds = Credentials.from_authorized_user_info(token_data)

        if creds.expired and creds.refresh_token:
            logger.debug("Refreshing expired Google Drive credentials")
            creds.refresh(Request())

        return build("drive", "v3", credentials=creds)

    def _list_files(self, service: Any) -> list[dict[str, Any]]:
        """List all files in the configured Google Drive folder.

        Handles pagination via nextPageToken.
        """
        all_files: list[dict[str, Any]] = []
        page_token: str | None = None
        query = f"'{self.config.folder_id}' in parents and trashed = false"

        while True:
            request_kwargs: dict[str, Any] = {
                "q": query,
                "fields": _FILE_FIELDS,
                "pageSize": 100,
            }
            if page_token is not None:
                request_kwargs["pageToken"] = page_token

            response: dict[str, Any] = service.files().list(**request_kwargs).execute()
            files = response.get("files", [])
            all_files.extend(files)

            page_token = response.get("nextPageToken")
            if page_token is None:
                break

        logger.info(
            "Listed %d files in Drive folder %s",
            len(all_files),
            self.config.folder_id,
        )
        return all_files

    def _download_file(
        self, service: Any, file_meta: dict[str, Any]
    ) -> tuple[bytes, str] | None:
        """Download file content from Google Drive.

        Returns (content_bytes, mime_type) or None if the file type
        is unsupported.
        """
        file_id: str = file_meta["id"]
        file_name: str = file_meta["name"]
        mime_type: str = file_meta["mimeType"]

        if mime_type in _SUPPORTED_MIME_TYPES:
            logger.debug("Downloading file %s (%s)", file_name, mime_type)
            content: bytes = service.files().get_media(fileId=file_id).execute()
            return content, mime_type

        if mime_type == _GOOGLE_DOCS_MIME_TYPE:
            logger.debug("Exporting Google Doc %s as PDF", file_name)
            content = (
                service.files()
                .export_media(fileId=file_id, mimeType="application/pdf")
                .execute()
            )
            return content, "application/pdf"

        logger.warning(
            "Skipping unsupported file type %s for file %s (%s)",
            mime_type,
            file_name,
            file_id,
        )
        return None
