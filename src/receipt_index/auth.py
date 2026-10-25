"""OAuth2 authentication flows for external services."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class AuthError(Exception):
    """Raised when an authentication flow fails."""


def run_gdrive_auth(client_credentials_path: str) -> str:
    """Run the Google Drive OAuth2 authorization flow.

    Opens a browser for the user to authorize read-only Drive access.
    Returns a JSON string containing the OAuth2 credentials (including
    a refresh token) suitable for storing as an environment variable.

    Args:
        client_credentials_path: Path to the ``client_secret.json`` file
            downloaded from Google Cloud Console.

    Returns:
        JSON string with the serialized OAuth2 credentials.

    Raises:
        AuthError: If the credentials file is invalid, missing, or the
            authorization flow is cancelled/fails.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials_file = Path(client_credentials_path)
    if not credentials_file.is_file():
        msg = f"Client credentials file not found: {client_credentials_path}"
        raise AuthError(msg)

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_file),
            scopes=_GDRIVE_SCOPES,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        msg = f"Invalid client credentials file: {exc}"
        raise AuthError(msg) from exc

    logger.info("Opening browser for Google Drive authorization...")

    try:
        credentials = flow.run_local_server(port=0)
    except Exception as exc:
        msg = f"Authorization flow failed: {exc}"
        raise AuthError(msg) from exc

    token_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes) if credentials.scopes else _GDRIVE_SCOPES,
    }

    return json.dumps(token_data)
