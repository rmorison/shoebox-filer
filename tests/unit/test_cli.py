"""Tests for receipt_index.cli."""

from __future__ import annotations

import json
import logging
import textwrap
from collections.abc import Iterator  # noqa: TC003
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path  # noqa: TC003
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from click.testing import CliRunner

from receipt_index.cli import cli
from receipt_index.config import AppConfig, ImapSourceConfig, LoggingConfig
from receipt_index.models import IngestLogEntry, Receipt
from receipt_index.pipeline import IngestResult

_RECEIPT_ROW: dict[str, Any] = {
    "id": UUID("019572a0-0000-7000-8000-000000000001"),
    "source_id": "<msg-1@example.com>",
    "source_type": "imap",
    "source_name": "personal-email",
    "vendor": "Amazon",
    "amount": Decimal("42.99"),
    "currency": "USD",
    "receipt_date": date(2025, 6, 15),
    "description": "Python Cookbook",
    "confidence": 0.95,
    "pdf_path": "2025/06/2025-06-15__amazon__42.99.pdf",
    "email_subject": "Your Amazon.com order",
    "email_sender": "no-reply@amazon.com",
    "email_date": datetime(2025, 6, 15, 10, 30, 0, tzinfo=UTC),
    "file_name": None,
    "created_at": datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC),
    "updated_at": datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC),
}

_SAMPLE_RECEIPT = Receipt.model_validate(_RECEIPT_ROW)

_FAILURE_ROW: dict[str, Any] = {
    "id": UUID("019572a0-0000-7000-8000-000000000099"),
    "source_id": "<fail-1@example.com>",
    "source_type": "imap",
    "status": "failed",
    "receipt_id": None,
    "vendor": None,
    "amount": None,
    "email_subject": "Your shipping update",
    "email_sender": "noreply@example.com",
    "email_date": datetime(2025, 6, 15, 10, 30, 0, tzinfo=UTC),
    "error_message": "amount > 0 validation failed",
    "created_at": datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC),
}

_SAMPLE_FAILURE = IngestLogEntry.model_validate(_FAILURE_ROW)

_MINIMAL_CONFIG_YAML = textwrap.dedent("""\
    sources:
      - name: test-email
        type: imap
        host: mail.example.com
        username: user@example.com
        password: secret
        folder: INBOX

    database:
      url: postgresql://localhost/test

    store:
      path: ./data/receipts

    llm:
      model: claude-haiku-4-5-20251001
      api_key: test-key

    logging:
      level: INFO
""")


_NOISY_LOGGERS = ("pdfminer", "fontTools", "PIL")
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


@pytest.fixture(autouse=True)
def isolated_logging() -> Iterator[None]:
    """Give every test an unconfigured root logger, restoring global state after.

    Logger levels and handlers are process-global, and ``logging.basicConfig``
    is a no-op once the root logger has handlers (pytest installs its own), so
    the root logger is emptied for the duration of the test. Autouse because
    ``_configure_logging`` runs ``basicConfig(force=True)`` on every CLI
    invocation — any test driving a command mutates global logging state and
    must have it restored, not just the logging-focused tests.
    """
    tracked = (*_NOISY_LOGGERS, "receipt_index")
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_root_level = root.level
    saved_levels = {name: logging.getLogger(name).level for name in tracked}

    root.handlers = []
    root.setLevel(logging.WARNING)
    for name in tracked:
        logging.getLogger(name).setLevel(logging.NOTSET)
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_root_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """Write a minimal config file and return its path."""
    config_path = tmp_path / "receipt-index.yaml"
    config_path.write_text(_MINIMAL_CONFIG_YAML)
    return config_path


def _make_config() -> AppConfig:
    """Build an AppConfig for testing without touching the filesystem."""
    return AppConfig(
        sources=[
            ImapSourceConfig(
                name="test-email",
                type="imap",
                host="mail.example.com",
                username="user@example.com",
                password="secret",  # pragma: allowlist secret
                folder="INBOX",
            ),
        ],
        database={"url": "postgresql://localhost/test"},  # type: ignore[arg-type]
        store={"path": "./data/receipts"},  # type: ignore[arg-type]
        llm={  # type: ignore[arg-type]
            "model": "claude-haiku-4-5-20251001",
            "api_key": "test-key",  # pragma: allowlist secret
        },
        logging={"level": "INFO"},  # type: ignore[arg-type]
    )


class TestIngestCommand:
    """Tests for the ingest CLI command."""

    @patch("receipt_index.pipeline.run_ingest")
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.store.LocalFileStore")
    @patch("receipt_index.extraction.create_extraction_agent")
    @patch("receipt_index.adapters.imap.ImapAdapter")
    @patch("receipt_index.cli.load_config")
    def test_ingest_success(
        self,
        mock_load_config: MagicMock,
        mock_adapter_cls: MagicMock,
        mock_create_agent: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_conn: MagicMock,
        mock_run: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        mock_run.return_value = IngestResult(processed=3, skipped=0, failed=0)
        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_file), "ingest"])
        assert result.exit_code == 0, result.output
        assert "Processed: 3" in result.output

    @patch("receipt_index.pipeline.run_ingest")
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.store.LocalFileStore")
    @patch("receipt_index.extraction.create_extraction_agent")
    @patch("receipt_index.adapters.imap.ImapAdapter")
    @patch("receipt_index.cli.load_config")
    def test_ingest_dry_run_passes_flag(
        self,
        mock_load_config: MagicMock,
        mock_adapter_cls: MagicMock,
        mock_create_agent: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_conn: MagicMock,
        mock_run: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        mock_run.return_value = IngestResult(skipped=5)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(config_file), "ingest", "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_run.call_args
        assert kwargs["dry_run"] is True

    @patch("receipt_index.pipeline.run_ingest")
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.store.LocalFileStore")
    @patch("receipt_index.extraction.create_extraction_agent")
    @patch("receipt_index.adapters.imap.ImapAdapter")
    @patch("receipt_index.cli.load_config")
    def test_ingest_limit_passes_value(
        self,
        mock_load_config: MagicMock,
        mock_adapter_cls: MagicMock,
        mock_create_agent: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_conn: MagicMock,
        mock_run: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        mock_run.return_value = IngestResult(processed=3)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(config_file), "ingest", "--limit", "3"]
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_run.call_args
        assert kwargs["limit"] == 3

    @patch("receipt_index.pipeline.run_ingest")
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.store.LocalFileStore")
    @patch("receipt_index.extraction.create_extraction_agent")
    @patch("receipt_index.adapters.imap.ImapAdapter")
    @patch("receipt_index.cli.load_config")
    def test_ingest_exits_1_on_failures(
        self,
        mock_load_config: MagicMock,
        mock_adapter_cls: MagicMock,
        mock_create_agent: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_conn: MagicMock,
        mock_run: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        mock_run.return_value = IngestResult(processed=1, failed=2)
        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_file), "ingest"])
        assert result.exit_code != 0

    @patch("receipt_index.pipeline.run_ingest")
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.store.LocalFileStore")
    @patch("receipt_index.extraction.create_extraction_agent")
    @patch("receipt_index.adapters.imap.ImapAdapter")
    @patch("receipt_index.cli.load_config")
    def test_ingest_source_filter(
        self,
        mock_load_config: MagicMock,
        mock_adapter_cls: MagicMock,
        mock_create_agent: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_conn: MagicMock,
        mock_run: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        mock_run.return_value = IngestResult(processed=2)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_file), "ingest", "--source", "test-email"],
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_run.call_args
        assert kwargs["source_name"] == "test-email"

    @patch("receipt_index.cli.load_config")
    def test_ingest_source_not_found(
        self,
        mock_load_config: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_file), "ingest", "--source", "nonexistent"],
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    @patch("receipt_index.extraction.create_extraction_agent")
    @patch("receipt_index.cli.load_config")
    @patch("receipt_index.pipeline.run_ingest")
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.store.LocalFileStore")
    @patch("receipt_index.adapters.gdrive.GdriveAdapter", autospec=True)
    def test_ingest_gdrive_source(
        self,
        mock_gdrive_cls: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_conn: MagicMock,
        mock_run: MagicMock,
        mock_load_config: MagicMock,
        mock_create_agent: MagicMock,
        config_file: Path,
    ) -> None:
        """GDrive sources should be ingested via GdriveAdapter."""
        from receipt_index.config import GdriveSourceConfig

        config = _make_config()
        gdrive_source = GdriveSourceConfig(
            name="scanned-receipts",
            type="gdrive",
            folder_id="abc123",
            token_json="{}",
        )
        config_with_gdrive = config.model_copy(update={"sources": [gdrive_source]})
        mock_load_config.return_value = config_with_gdrive
        mock_run.return_value = IngestResult(processed=0, skipped=0, failed=0)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "ingest",
                "--source",
                "scanned-receipts",
            ],
        )
        assert result.exit_code == 0, result.output
        mock_gdrive_cls.assert_called_once_with(gdrive_source)


class TestSearchCommand:
    """Tests for the search CLI command."""

    @patch("receipt_index.repository.search_receipts", return_value=[_SAMPLE_RECEIPT])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_vendor_filter(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(config_file), "search", "--vendor", "amazon"]
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_search.call_args
        assert kwargs["vendor"] == "amazon"

    @patch("receipt_index.repository.search_receipts", return_value=[_SAMPLE_RECEIPT])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_source_filter(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "search",
                "--source",
                "personal-email",
            ],
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_search.call_args
        assert kwargs["source_name"] == "personal-email"

    @patch("receipt_index.repository.search_receipts", return_value=[_SAMPLE_RECEIPT])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_json_output(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(config_file), "search", "--output", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["vendor"] == "Amazon"

    @patch("receipt_index.repository.search_receipts", return_value=[_SAMPLE_RECEIPT])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_text_has_table_headers(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_file), "search"])
        assert result.exit_code == 0, result.output
        assert "ID" in result.output
        assert "Date" in result.output
        assert "Vendor" in result.output
        assert "Amount" in result.output
        assert "Amazon" in result.output

    @patch("receipt_index.repository.search_receipts", return_value=[])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_no_results(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_file), "search"])
        assert result.exit_code == 0
        assert "No receipts found" in result.output

    @patch("receipt_index.repository.search_receipts", return_value=[_SAMPLE_RECEIPT])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_amount_range(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "search",
                "--amount-min",
                "10",
                "--amount-max",
                "100",
            ],
        )
        assert result.exit_code == 0
        _, kwargs = mock_search.call_args
        assert kwargs["amount_min"] == Decimal("10")
        assert kwargs["amount_max"] == Decimal("100")

    @patch("receipt_index.repository.search_receipts", return_value=[_SAMPLE_RECEIPT])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_date_range(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "search",
                "--date-from",
                "2025-01-01",
                "--date-to",
                "2025-12-31",
            ],
        )
        assert result.exit_code == 0
        _, kwargs = mock_search.call_args
        assert kwargs["date_from"] == date(2025, 1, 1)
        assert kwargs["date_to"] == date(2025, 12, 31)

    @patch("receipt_index.cli.load_config")
    def test_search_invalid_amount(
        self,
        mock_load_config: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_file), "search", "--amount", "not-a-number"],
        )
        assert result.exit_code != 0

    @patch("receipt_index.cli.load_config")
    def test_search_amount_mutually_exclusive_with_range(
        self,
        mock_load_config: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "search",
                "--amount",
                "42.99",
                "--amount-min",
                "10",
            ],
        )
        assert result.exit_code != 0
        assert "--amount cannot be combined" in result.output


class TestFailuresCommand:
    """Tests for the failures CLI command."""

    @patch(
        "receipt_index.repository.get_ingest_failures",
        return_value=[_SAMPLE_FAILURE],
    )
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_failures_text_output(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_file), "failures"])
        assert result.exit_code == 0
        assert "noreply@example.com" in result.output
        assert "Your shipping update" in result.output
        assert "1 failed ingest(s)" in result.output

    @patch(
        "receipt_index.repository.get_ingest_failures",
        return_value=[_SAMPLE_FAILURE],
    )
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_failures_json_output(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(config_file), "failures", "--output", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["status"] == "failed"
        assert data[0]["error_message"] == "amount > 0 validation failed"

    @patch("receipt_index.repository.get_ingest_failures", return_value=[])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_failures_empty(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_file), "failures"])
        assert result.exit_code == 0
        assert "No failed ingests" in result.output


class TestShowCommand:
    """Tests for the show CLI command."""

    @patch("receipt_index.repository.get_receipt_by_id", return_value=_SAMPLE_RECEIPT)
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_show_text_output(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "show",
                "019572a0-0000-7000-8000-000000000001",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Amazon" in result.output
        assert "42.99" in result.output
        assert "Source Name:  personal-email" in result.output

    @patch("receipt_index.repository.get_receipt_by_id", return_value=_SAMPLE_RECEIPT)
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_show_json_output(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "show",
                "019572a0-0000-7000-8000-000000000001",
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["vendor"] == "Amazon"

    @patch("receipt_index.cli.load_config")
    def test_show_invalid_uuid(
        self,
        mock_load_config: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(config_file), "show", "not-a-uuid"]
        )
        assert result.exit_code != 0

    @patch("receipt_index.repository.get_receipt_by_id", return_value=None)
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_show_not_found(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "show",
                "019572a0-0000-7000-8000-000000000099",
            ],
        )
        assert result.exit_code != 0
        assert "not found" in result.output


class TestLoggingSetup:
    """Tests for CLI logging configuration."""

    @patch("receipt_index.repository.search_receipts", return_value=[])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_noisy_libraries_clamped_at_info(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
        isolated_logging: None,
    ) -> None:
        """At app level INFO, pdfminer/fontTools/PIL are held at WARNING."""
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_file), "search"])
        assert result.exit_code == 0, result.output
        for name in _NOISY_LOGGERS:
            assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING

    @patch("receipt_index.repository.search_receipts", return_value=[])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_noisy_libraries_clamped_at_debug(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
        isolated_logging: None,
    ) -> None:
        """At app level DEBUG, app loggers emit DEBUG but libraries stay quiet."""
        config = _make_config().model_copy(
            update={"logging": LoggingConfig(level="DEBUG")}
        )
        mock_load_config.return_value = config
        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_file), "search"])
        assert result.exit_code == 0, result.output

        assert logging.getLogger("receipt_index").getEffectiveLevel() == logging.DEBUG
        assert (
            logging.getLogger("receipt_index.pipeline").getEffectiveLevel()
            == logging.DEBUG
        )
        for name in _NOISY_LOGGERS:
            assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
            assert not logging.getLogger(name).isEnabledFor(logging.INFO)

    @patch("receipt_index.repository.search_receipts", return_value=[])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_app_logging_level_and_format_unchanged(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
        isolated_logging: None,
    ) -> None:
        """receipt_index.* loggers keep the configured level and log format."""
        mock_load_config.return_value = _make_config()
        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_file), "search"])
        assert result.exit_code == 0, result.output

        app_logger = logging.getLogger("receipt_index")
        assert app_logger.level == logging.NOTSET  # not clamped explicitly
        assert app_logger.getEffectiveLevel() == logging.INFO

        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(root.handlers) == 1
        formatter = root.handlers[0].formatter
        assert formatter is not None
        record = logging.LogRecord(
            name="receipt_index.pipeline",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="ingest complete",
            args=None,
            exc_info=None,
        )
        assert formatter.format(record).endswith(
            "INFO receipt_index.pipeline: ingest complete"
        )
        assert formatter.format(record) == logging.Formatter(_LOG_FORMAT).format(record)


class TestConfigOption:
    """Tests for the --config CLI option."""

    def test_missing_config_file_shows_error(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(tmp_path / "nonexistent.yaml"), "search"]
        )
        assert result.exit_code != 0
        assert "Error" in result.output


class TestMcpCommand:
    """Tests for the mcp CLI command."""

    def test_help(self) -> None:
        result = CliRunner().invoke(cli, ["mcp", "--help"])
        assert result.exit_code == 0, result.output
        assert "read-only MCP server" in result.output
        assert "--access-log" in result.output
        assert "--max-content-bytes" in result.output

    @patch("receipt_index.mcp_server.build_server")
    @patch("receipt_index.mcp_server.startup_check")
    @patch("receipt_index.cli.load_config")
    def test_refuses_writable_role(
        self,
        mock_load_config: MagicMock,
        mock_check: MagicMock,
        mock_build: MagicMock,
        tmp_path: Path,
    ) -> None:
        from receipt_index.mcp_server import RoleCheckError

        mock_load_config.return_value = _make_config()
        mock_check.side_effect = RoleCheckError("the database role can write")
        result = CliRunner().invoke(
            cli, ["mcp", "--access-log", str(tmp_path / "log.jsonl")]
        )
        assert result.exit_code == 1
        assert "the database role can write" in result.output
        mock_build.assert_not_called()

    @patch("receipt_index.mcp_server.build_server")
    @patch("receipt_index.mcp_server.startup_check")
    @patch("receipt_index.cli.load_config")
    def test_refuses_unwritable_access_log(
        self,
        mock_load_config: MagicMock,
        mock_check: MagicMock,
        mock_build: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        blocker = tmp_path / "blocker"
        blocker.write_text("a file")
        result = CliRunner().invoke(
            cli, ["mcp", "--access-log", str(blocker / "log.jsonl")]
        )
        assert result.exit_code == 1
        assert "not writable" in result.output
        mock_build.assert_not_called()

    @patch("receipt_index.mcp_server.build_server")
    @patch("receipt_index.mcp_server.startup_check")
    @patch("receipt_index.cli.load_config")
    def test_prefers_mcp_database_url(
        self,
        mock_load_config: MagicMock,
        mock_check: MagicMock,
        mock_build: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_load_config.return_value = _make_config()
        reader_url = "postgresql://reader@localhost/test"  # pragma: allowlist secret
        monkeypatch.setenv("RECEIPT_INDEX_MCP_DATABASE_URL", reader_url)
        result = CliRunner().invoke(
            cli,
            [
                "mcp",
                "--access-log",
                str(tmp_path / "log.jsonl"),
                "--max-content-bytes",
                "2048",
            ],
        )
        assert result.exit_code == 0, result.output
        mock_check.assert_called_once_with(reader_url)
        kwargs = mock_build.call_args.kwargs
        assert kwargs["database_url"] == reader_url
        assert kwargs["max_content_bytes"] == 2048
        assert kwargs["access_log"].path == tmp_path / "log.jsonl"
        mock_build.return_value.run.assert_called_once_with("stdio")

    @patch("receipt_index.mcp_server.build_server")
    @patch("receipt_index.mcp_server.startup_check")
    @patch("receipt_index.cli.load_config")
    def test_falls_back_to_config_database_url(
        self,
        mock_load_config: MagicMock,
        mock_check: MagicMock,
        mock_build: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_load_config.return_value = _make_config()
        monkeypatch.delenv("RECEIPT_INDEX_MCP_DATABASE_URL", raising=False)
        result = CliRunner().invoke(
            cli, ["mcp", "--access-log", str(tmp_path / "log.jsonl")]
        )
        assert result.exit_code == 0, result.output
        mock_check.assert_called_once_with("postgresql://localhost/test")
        assert mock_build.call_args.kwargs["max_content_bytes"] == 10 * 1024 * 1024


class TestMcpCommandDatabaseErrors:
    """The mcp command turns database errors at startup into a short message."""

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            ("OperationalError", "cannot connect to the database"),
            ("ProgrammingError", "startup check failed"),
        ],
    )
    @patch("receipt_index.mcp_server.build_server")
    @patch("receipt_index.mcp_server.startup_check")
    @patch("receipt_index.cli.load_config")
    def test_database_error_exits_1(
        self,
        mock_load_config: MagicMock,
        mock_check: MagicMock,
        mock_build: MagicMock,
        tmp_path: Path,
        error: str,
        message: str,
    ) -> None:
        import psycopg

        mock_load_config.return_value = _make_config()
        mock_check.side_effect = getattr(psycopg, error)("boom")
        result = CliRunner().invoke(
            cli, ["mcp", "--access-log", str(tmp_path / "log.jsonl")]
        )
        assert result.exit_code == 1
        assert message in result.output
        assert "Traceback" not in result.output
        mock_build.assert_not_called()


_PAID_RECEIPT = Receipt.model_validate(
    {
        **_RECEIPT_ROW,
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("50.00"),
        "paid_amount": Decimal("30.00"),
        "field_confidence": {
            "vendor": 0.95,
            "amount": 0.9,
            "paid_amount": 0.7,
            "date": 0.99,
        },
    }
)
_UNKNOWN_PAID_RECEIPT = Receipt.model_validate(
    {**_RECEIPT_ROW, "vendor": "Acme Parking (U.S.), LLC", "amount": Decimal("75.00")}
)


def _invoke(config_file: Path, *args: str) -> Any:
    return CliRunner().invoke(cli, ["--config", str(config_file), *args])


class TestPaidAmountOutput:
    """search and show show the effective amount, the paid amount and the total."""

    @patch("receipt_index.repository.search_receipts", return_value=[_PAID_RECEIPT])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_text_shows_effective_and_total(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        result = _invoke(config_file, "search")
        assert result.exit_code == 0, result.output
        header = result.output.splitlines()[0]
        assert header.split()[-2:] == ["Amount", "Total"]
        row = next(
            line for line in result.output.splitlines() if "Acme Parking" in line
        )
        assert row.split()[-2:] == ["30.00", "50.00"]

    @patch(
        "receipt_index.repository.search_receipts",
        return_value=[_UNKNOWN_PAID_RECEIPT],
    )
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_text_falls_back_to_total(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        result = _invoke(config_file, "search")
        assert result.exit_code == 0, result.output
        row = next(
            line for line in result.output.splitlines() if "Acme Parking" in line
        )
        assert row.split()[-2:] == ["75.00", "75.00"]

    @patch("receipt_index.repository.search_receipts", return_value=[_PAID_RECEIPT])
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_search_json_keeps_amount_and_adds_both(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        result = _invoke(config_file, "search", "--output", "json")
        assert result.exit_code == 0, result.output
        (item,) = json.loads(result.output)
        assert item["amount"] == "50.00"
        assert item["paid_amount"] == "30.00"
        assert item["effective_amount"] == "30.00"
        assert item["field_confidence"]["paid_amount"] == 0.7

    @patch("receipt_index.repository.get_receipt_by_id", return_value=_PAID_RECEIPT)
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_show_text_paid_known(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        result = _invoke(config_file, "show", "019572a0-0000-7000-8000-000000000001")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert "Amount:       30.00 USD (paid)" in lines
        assert "Paid:         30.00" in lines
        assert "Total:        50.00" in lines
        assert (
            "Field Conf:   vendor 0.95, amount 0.90, paid_amount 0.70, date 0.99"
            in lines
        )

    @patch(
        "receipt_index.repository.get_receipt_by_id",
        return_value=_UNKNOWN_PAID_RECEIPT,
    )
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_show_text_paid_unknown(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        result = _invoke(config_file, "show", "019572a0-0000-7000-8000-000000000001")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert "Amount:       75.00 USD (total; paid amount not known)" in lines
        assert "Paid:         — (not known)" in lines
        assert "Total:        75.00" in lines
        assert not any(line.startswith("Field Conf:") for line in lines)

    @patch("receipt_index.repository.get_receipt_by_id", return_value=_PAID_RECEIPT)
    @patch("receipt_index.db.get_connection")
    @patch("receipt_index.cli.load_config")
    def test_show_json_keeps_amount_and_adds_both(
        self,
        mock_load_config: MagicMock,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,
    ) -> None:
        mock_load_config.return_value = _make_config()
        result = _invoke(
            config_file,
            "show",
            "019572a0-0000-7000-8000-000000000001",
            "--output",
            "json",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["amount"] == "50.00"
        assert data["paid_amount"] == "30.00"
        assert data["effective_amount"] == "30.00"

    def test_search_help_says_effective_amount(self) -> None:
        result = CliRunner().invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "effective amount" in " ".join(result.output.split())
