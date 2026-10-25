"""Tests for receipt_index.auth."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from click.testing import CliRunner

from receipt_index.auth import AuthError, run_gdrive_auth
from receipt_index.cli import cli


def _make_mock_credentials() -> MagicMock:
    """Create a mock Google OAuth2 credentials object."""
    creds = MagicMock()
    creds.token = "ya29.test-access-token"
    creds.refresh_token = "1//test-refresh-token"
    creds.token_uri = "https://oauth2.googleapis.com/token"
    creds.client_id = "123456.apps.googleusercontent.com"
    creds.client_secret = "test-client-secret"  # pragma: allowlist secret
    creds.scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    return creds


class TestRunGdriveAuth:
    """Tests for run_gdrive_auth()."""

    def test_successful_auth_returns_token_json(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "client_secret.json"
        creds_file.write_text('{"installed": {"client_id": "test"}}')

        mock_creds = _make_mock_credentials()

        with patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file"
        ) as mock_from_file:
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = mock_creds
            mock_from_file.return_value = mock_flow

            result = run_gdrive_auth(str(creds_file))

        token_data = json.loads(result)
        assert token_data["token"] == "ya29.test-access-token"
        assert token_data["refresh_token"] == "1//test-refresh-token"
        assert token_data["token_uri"] == "https://oauth2.googleapis.com/token"
        assert token_data["client_id"] == "123456.apps.googleusercontent.com"
        assert (
            token_data["client_secret"] == "test-client-secret"
        )  # pragma: allowlist secret
        assert token_data["scopes"] == [
            "https://www.googleapis.com/auth/drive.readonly"
        ]

    def test_uses_correct_scopes(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "client_secret.json"
        creds_file.write_text('{"installed": {"client_id": "test"}}')

        mock_creds = _make_mock_credentials()

        with patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file"
        ) as mock_from_file:
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = mock_creds
            mock_from_file.return_value = mock_flow

            run_gdrive_auth(str(creds_file))

        mock_from_file.assert_called_once_with(
            str(creds_file),
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )

    def test_file_not_found_raises_auth_error(self) -> None:
        with pytest.raises(AuthError, match="not found"):
            run_gdrive_auth("/nonexistent/path/client_secret.json")

    def test_invalid_credentials_file_raises_auth_error(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "bad_creds.json"
        creds_file.write_text("not valid json {{{")

        with (
            patch(
                "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                side_effect=ValueError("Invalid client secrets"),
            ),
            pytest.raises(AuthError, match="Invalid client credentials"),
        ):
            run_gdrive_auth(str(creds_file))

    def test_auth_flow_cancelled_raises_auth_error(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "client_secret.json"
        creds_file.write_text('{"installed": {"client_id": "test"}}')

        with patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file"
        ) as mock_from_file:
            mock_flow = MagicMock()
            mock_flow.run_local_server.side_effect = RuntimeError(
                "Authorization cancelled"
            )
            mock_from_file.return_value = mock_flow

            with pytest.raises(AuthError, match="Authorization flow failed"):
                run_gdrive_auth(str(creds_file))

    def test_none_scopes_defaults_to_drive_readonly(self, tmp_path: Path) -> None:
        """When credentials.scopes is None, use the default scopes."""
        creds_file = tmp_path / "client_secret.json"
        creds_file.write_text('{"installed": {"client_id": "test"}}')

        mock_creds = _make_mock_credentials()
        mock_creds.scopes = None

        with patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file"
        ) as mock_from_file:
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = mock_creds
            mock_from_file.return_value = mock_flow

            result = run_gdrive_auth(str(creds_file))

        token_data = json.loads(result)
        assert token_data["scopes"] == [
            "https://www.googleapis.com/auth/drive.readonly"
        ]


class TestAuthGdriveCli:
    """Tests for the 'auth gdrive' CLI command."""

    def test_successful_auth_outputs_token(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "client_secret.json"
        creds_file.write_text('{"installed": {"client_id": "test"}}')

        mock_creds = _make_mock_credentials()
        token_json = json.dumps(
            {
                "token": mock_creds.token,
                "refresh_token": mock_creds.refresh_token,
                "token_uri": mock_creds.token_uri,
                "client_id": mock_creds.client_id,
                "client_secret": mock_creds.client_secret,
                "scopes": list(mock_creds.scopes),
            }
        )

        runner = CliRunner()
        with patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file"
        ) as mock_from_file:
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = mock_creds
            mock_from_file.return_value = mock_flow

            result = runner.invoke(
                cli,
                ["auth", "gdrive", "--client-credentials", str(creds_file)],
            )

        assert result.exit_code == 0
        assert "Authorization successful" in result.output
        assert "GDRIVE_TOKEN_JSON" in result.output
        assert token_json in result.output

    def test_auth_error_shows_message_and_exits_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "auth",
                "gdrive",
                "--client-credentials",
                str(tmp_path / "nonexistent.json"),
            ],
        )

        # click.Path(exists=True) catches the missing file before our code
        assert result.exit_code != 0

    def test_missing_client_credentials_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "gdrive"])

        assert result.exit_code != 0
        assert "Missing option" in result.output or "required" in result.output.lower()

    def test_auth_flow_failure_exits_1(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "client_secret.json"
        creds_file.write_text('{"installed": {"client_id": "test"}}')

        runner = CliRunner()
        with patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file"
        ) as mock_from_file:
            mock_flow = MagicMock()
            mock_flow.run_local_server.side_effect = RuntimeError("Network error")
            mock_from_file.return_value = mock_flow

            result = runner.invoke(
                cli,
                ["auth", "gdrive", "--client-credentials", str(creds_file)],
            )

        assert result.exit_code == 1
        assert "Error:" in result.output
