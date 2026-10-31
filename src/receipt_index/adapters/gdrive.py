"""Google Drive source adapter.

Reads the Drive changes feed from a stored page token, filtered to the
dropoff folder. With no token yet (or an invalid one, or a different
account), it bootstraps: it takes a start token *before* listing the folder,
so a file uploaded during the listing is in both and idempotency removes the
duplicate (see the always-on ingest design, section 4, and ADR-0008).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self

from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.models import ItemRef, RawReceipt, SourceRead

if TYPE_CHECKING:
    from datetime import date
    from types import TracebackType

    from receipt_index.config import GdriveSourceConfig
    from receipt_index.models import SyncState

logger = logging.getLogger(__name__)

_SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
}

_GOOGLE_DOCS_MIME_TYPE = "application/vnd.google-apps.document"
_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

_FILE_FIELDS = "nextPageToken, files(id, name, mimeType, modifiedTime)"
_CHANGE_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,time,"
    "file(id,name,mimeType,parents,trashed,createdTime,modifiedTime,sha256Checksum))"
)

UNSUPPORTED_REASON = "unsupported file type"

# What fetch_by_id asks files.get for: enough to build a ref.
_GET_FIELDS = "id, name, mimeType, modifiedTime"

# A changes feed longer than this is treated as an API error rather than paged
# for ever (at 1000 changes a page, that is ten million changes).
_MAX_CHANGE_PAGES = 10_000
# The same guard for the bootstrap listing (at 100 files a page, a million).
_MAX_LIST_PAGES = 10_000

# 403 reasons that refuse the whole account or project, not one file.
_ACCOUNT_WIDE_403 = (
    b"rateLimitExceeded",
    b"userRateLimitExceeded",
    b"dailyLimitExceeded",
    b"quotaExceeded",
    b"insufficientPermissions",
    b"domainPolicy",
)


def _parse_drive_date(date_str: str) -> datetime:
    """Parse an RFC 3339 timestamp from the Google Drive API.

    Google Drive returns timestamps like '2024-01-15T10:30:00.000Z'.
    """
    # Replace Z suffix with +00:00 for fromisoformat compatibility
    cleaned = date_str.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned).astimezone(UTC)


def _is_invalid_token_error(exc: Exception) -> bool:
    """Whether an API error means the stored page token is no longer valid.

    Drive v3 answers a bad or expired ``pageToken`` with HTTP 400 "Invalid
    Value" naming the parameter. A 404 or 410, which some Google APIs use, is
    treated the same way as a precaution: the cost is one folder listing.
    """
    from googleapiclient.errors import HttpError

    if not isinstance(exc, HttpError):
        return False
    status = int(exc.resp.status)
    if status in (404, 410):
        return True
    content = exc.content or b""
    if isinstance(content, str):
        content = content.encode()
    return status == 400 and b"pageToken" in content


def _is_not_found(exc: Exception) -> bool:
    """Whether an API error says the file doesn't exist (or isn't visible)."""
    from googleapiclient.errors import HttpError

    return isinstance(exc, HttpError) and int(exc.resp.status) == 404


def _is_transient_error(exc: Exception) -> bool:
    """Whether a download error is the service's, not the file's.

    Rate limits (429), revoked credentials (401), a 403 naming an
    account-wide limit or policy, server errors (5xx) and network or
    credential failures are the service's; a 404, or a 403 on one file, is
    that file's.
    """
    import http.client

    import httplib2
    from google.auth.exceptions import RefreshError, TransportError
    from googleapiclient.errors import HttpError

    # Network failures (OSError, a body cut off mid-download, a DNS failure
    # from httplib2, a token refresh that can't reach Google) and a refused
    # refresh (an auth problem for the whole source) are not the file's fault.
    if isinstance(
        exc,
        OSError
        | http.client.HTTPException
        | httplib2.HttpLib2Error
        | TransportError
        | RefreshError,
    ):
        return True
    if not isinstance(exc, HttpError):
        return False
    status = int(exc.resp.status)
    # 401: the credentials no longer work, for every file.
    if status in (401, 429) or status >= 500:
        return True
    content = exc.content or b""
    if isinstance(content, str):
        content = content.encode()
    return status == 403 and any(reason in content for reason in _ACCOUNT_WIDE_403)


def _skip_reason(mime_type: str) -> str | None:
    if mime_type in _SUPPORTED_MIME_TYPES or mime_type == _GOOGLE_DOCS_MIME_TYPE:
        return None
    return UNSUPPORTED_REASON


class GdriveAdapter:
    """Read new receipt files from a Google Drive folder."""

    source_type = "gdrive"

    def __init__(self, config: GdriveSourceConfig, *, settle_seconds: int = 60) -> None:
        self.config = config
        self.settle = timedelta(seconds=settle_seconds)
        self._service: Any = None

    @property
    def folder(self) -> str:
        """The folder ID the sync-state row is keyed on."""
        return self.config.folder_id

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
        """Drop the API client."""
        self._service = None

    def _session(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    # -- reading ---------------------------------------------------------

    def list_new(self, state: SyncState | None) -> SourceRead:
        """List files new since the stored token, or bootstrap without one."""
        service = self._session()
        account_id = self._account_id(service)
        token = state.drive_page_token if state else None
        stored_account = state.drive_account_id if state else None

        if token is None or stored_account is None:
            return self._bootstrap(service, account_id, reset=None)
        if stored_account != account_id:
            logger.warning(
                "Drive account for source %s changed: re-bootstrapping",
                self.config.name,
            )
            return self._bootstrap(service, account_id, reset="account changed")
        try:
            return self._poll(service, token, account_id)
        except Exception as exc:
            if not _is_invalid_token_error(exc):
                raise
            logger.warning(
                "Drive page token for source %s is invalid (%s): re-bootstrapping",
                self.config.name,
                exc,
            )
            return self._bootstrap(service, account_id, reset="invalid page token")

    def list_range(self, since: date | None, until: date | None) -> list[ItemRef]:
        """List the dropoff's files created from ``since`` to ``until``, inclusive.

        Days are UTC. Folders are dropped, and a file still inside the
        settle time is left for a later run, as the bootstrap does. Never
        touches the sync state.
        """
        clauses: list[str] = []
        if since is not None:
            clauses.append(f"createdTime >= '{since.isoformat()}T00:00:00Z'")
        if until is not None:
            end = until + timedelta(days=1)
            clauses.append(f"createdTime < '{end.isoformat()}T00:00:00Z'")
        files = self._list_files(self._session(), extra=clauses)
        now = datetime.now(tz=UTC)
        items: list[ItemRef] = []
        for file_meta in files:
            if file_meta.get("mimeType") == _FOLDER_MIME_TYPE:
                continue
            modified = file_meta.get("modifiedTime")
            if modified and now - _parse_drive_date(modified) < self.settle:
                logger.info(
                    "Drive file %s is inside the settle time; left for later",
                    file_meta.get("id"),
                )
                continue
            items.append(self._ref(file_meta))
        return items

    @staticmethod
    def _account_id(service: Any) -> str:
        """The stable ID of the account whose change log the token belongs to."""
        about: dict[str, Any] = (
            service.about().get(fields="user(permissionId)").execute()
        )
        return str(about["user"]["permissionId"])

    def _bootstrap(
        self, service: Any, account_id: str, *, reset: str | None
    ) -> SourceRead:
        """Take a start token first, then list the folder."""
        start: dict[str, Any] = service.changes().getStartPageToken().execute()
        files = self._list_files(service)

        now = datetime.now(tz=UTC)
        items: list[ItemRef] = []
        held_back = False
        for file_meta in files:
            if file_meta.get("mimeType") == _FOLDER_MIME_TYPE:
                continue
            # A listing has no change time; settle on the file's modifiedTime.
            modified = file_meta.get("modifiedTime")
            if modified and now - _parse_drive_date(modified) < self.settle:
                held_back = True
                continue
            items.append(self._ref(file_meta))

        return SourceRead(
            items=items,
            reset=reset,
            drive_page_token=str(start["startPageToken"]),
            drive_account_id=account_id,
            held_back=held_back,
        )

    def _poll(self, service: Any, token: str, account_id: str) -> SourceRead:
        """Read the changes feed from ``token`` to its end."""
        latest: dict[str, dict[str, Any]] = {}
        page_token = token
        for _page in range(_MAX_CHANGE_PAGES):
            response: dict[str, Any] = (
                service.changes()
                .list(
                    pageToken=page_token,
                    spaces="drive",
                    fields=_CHANGE_FIELDS,
                    pageSize=1000,
                )
                .execute()
            )
            for change in response.get("changes", []):
                # Changes arrive in order: the last one for a file wins.
                latest[str(change.get("fileId"))] = change
            next_page = response.get("nextPageToken")
            if next_page is None:
                new_token = str(response["newStartPageToken"])
                break
            if not isinstance(next_page, str) or next_page == page_token:
                raise RuntimeError(
                    f"Drive changes feed returned a bad page token: {next_page!r}"
                )
            page_token = next_page
        else:
            raise RuntimeError(
                f"Drive changes feed longer than {_MAX_CHANGE_PAGES} pages"
            )

        now = datetime.now(tz=UTC)
        items: list[ItemRef] = []
        held_back = False
        for change in latest.values():
            file_meta = change.get("file")
            if change.get("removed") or not file_meta:
                continue
            if self.config.folder_id not in file_meta.get("parents", []):
                continue
            if file_meta.get("trashed") or file_meta.get("mimeType") == (
                _FOLDER_MIME_TYPE
            ):
                continue
            # Settle on the server-assigned change time, not modifiedTime,
            # which a scanner app can set from its own clock.
            changed = change.get("time")
            if changed and now - _parse_drive_date(changed) < self.settle:
                held_back = True
                continue
            items.append(self._ref(file_meta))

        return SourceRead(
            items=items,
            drive_page_token=new_token,
            drive_account_id=account_id,
            held_back=held_back,
        )

    def _ref(self, file_meta: dict[str, Any]) -> ItemRef:
        modified = file_meta.get("modifiedTime")
        return ItemRef(
            source_id=str(file_meta["id"]),
            source_type="gdrive",
            date=_parse_drive_date(modified) if modified else None,
            file_name=file_meta.get("name"),
            drive_meta=file_meta,
            skip_reason=_skip_reason(str(file_meta.get("mimeType", ""))),
        )

    def fetch(self, ref: ItemRef) -> RawReceipt | None:
        """Download one file. None if its type can't be ingested."""
        if ref.drive_meta is None:
            raise ValueError(f"Drive ref without metadata: {ref.source_id}")
        try:
            result = self._download_file(self._session(), ref.drive_meta)
        except Exception as exc:
            if _is_transient_error(exc):
                raise SourceUnavailableError(
                    f"Drive download of {ref.source_id} failed: {exc}"
                ) from exc
            raise
        if result is None:
            return None
        content, mime_type = result
        # A native Google Doc arrives as its PDF export, which isn't the
        # original and has no stable hash; only a binary file's bytes are.
        native = ref.drive_meta.get("mimeType") == _GOOGLE_DOCS_MIME_TYPE
        return RawReceipt(
            source_id=ref.source_id,
            source_name=self.config.name,
            source_type="gdrive",
            date=ref.date or datetime.now(tz=UTC),
            file_name=ref.file_name,
            file_content=content,
            file_content_type=mime_type,
            raw_bytes=None if native else content,
        )

    def fetch_by_id(self, source_id: str) -> RawReceipt | None:
        """Get one file by its ID, wherever it is now, and download it.

        None if it no longer exists or its type can't be ingested. A service
        that can't be built (bad or missing credentials) is an outage for the
        whole source, never one file's fault.
        """
        try:
            service = self._session()
        except Exception as exc:
            raise SourceUnavailableError(
                f"Drive source {self.config.name} can't be reached: {exc}"
            ) from exc
        try:
            meta: dict[str, Any] = (
                service.files().get(fileId=source_id, fields=_GET_FIELDS).execute()
            )
        except Exception as exc:
            if _is_not_found(exc):
                return None
            if _is_transient_error(exc):
                raise SourceUnavailableError(
                    f"Drive lookup of {source_id} failed: {exc}"
                ) from exc
            raise
        ref = self._ref(meta)
        if ref.skip_reason is not None:
            return None
        return self.fetch(ref)

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

    def _list_files(
        self, service: Any, *, extra: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """List all files in the configured Google Drive folder.

        ``extra`` adds query clauses (a ``createdTime`` range). Handles
        pagination via nextPageToken.
        """
        all_files: list[dict[str, Any]] = []
        page_token: str | None = None
        query = " and ".join(
            [f"'{self.config.folder_id}' in parents", "trashed = false", *(extra or [])]
        )

        for _page in range(_MAX_LIST_PAGES):
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

            next_page = response.get("nextPageToken")
            if next_page is None:
                break
            if not isinstance(next_page, str) or next_page == page_token:
                raise RuntimeError(
                    f"Drive folder listing returned a bad page token: {next_page!r}"
                )
            page_token = next_page
        else:
            raise RuntimeError(
                f"Drive folder listing longer than {_MAX_LIST_PAGES} pages"
            )

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
