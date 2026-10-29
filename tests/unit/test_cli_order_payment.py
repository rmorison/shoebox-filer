"""search's order-number and payment filters, and the fields in its output.

The receipt data-model design, §4: ``--order-number`` matches exactly,
``--last4`` takes exactly four digits, ``--payer`` is a substring.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from click.testing import CliRunner

from receipt_index.cli import cli
from receipt_index.config import AppConfig, ImapSourceConfig
from receipt_index.models import Receipt

if TYPE_CHECKING:
    from pathlib import Path

_ROW: dict[str, Any] = {
    "id": UUID("019572a0-0000-7000-8000-000000000001"),
    "source_id": "<msg-1@example.com>",
    "source_type": "imap",
    "source_name": "personal-email",
    "vendor": "Acme Parking (U.S.), LLC",
    "amount": Decimal("50.00"),
    "paid_amount": Decimal("30.00"),
    "currency": "USD",
    "receipt_date": date(2026, 1, 15),
    "description": "Parking",
    "confidence": 0.95,
    "pdf_path": "2026/01/x.pdf",
    "email_subject": "Your receipt",
    "email_sender": "receipts@example.com",
    "email_date": datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
    "file_name": None,
    "order_number": "A-100",
    "payment_method": "card",
    "payment_last4": "1234",
    "payer": "Jane Doe",
    "created_at": datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
    "updated_at": datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
}
_RECEIPT = Receipt.model_validate(_ROW)
_BARE = Receipt.model_validate(
    {
        **_ROW,
        "order_number": None,
        "payment_method": None,
        "payment_last4": None,
        "payer": None,
    }
)


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "sources": [
                ImapSourceConfig(
                    name="personal-email",
                    host="mail.example.com",
                    username="user@example.com",
                    password="secret",  # pragma: allowlist secret
                )
            ],
            "database": {"url": "postgresql://localhost/test"},
            "llm": {"api_key": "test-key"},  # pragma: allowlist secret
        }
    )


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "receipt-index.yaml"
    path.write_text(
        textwrap.dedent("""\
        sources: []
        database:
          url: postgresql://localhost/test
        llm:
          api_key: test-key
        """)
    )
    return path


def _invoke(config_file: Path, *args: str) -> Any:
    with patch("receipt_index.cli.load_config", return_value=_config()):
        return CliRunner().invoke(cli, ["--config", str(config_file), *args])


class TestSearchFilters:
    @patch("receipt_index.repository.search_receipts", return_value=[_RECEIPT])
    @patch("receipt_index.db.get_connection")
    def test_passes_the_filters(
        self, mock_conn: MagicMock, mock_search: MagicMock, config_file: Path
    ) -> None:
        result = _invoke(
            config_file,
            "search",
            "--order-number",
            " A-100 ",
            "--last4",
            "1234",
            "--payer",
            "jane",
        )
        assert result.exit_code == 0, result.output
        kwargs = mock_search.call_args.kwargs
        assert kwargs["order_number"] == "A-100"
        assert kwargs["last4"] == "1234"
        assert kwargs["payer"] == "jane"

    @patch("receipt_index.repository.search_receipts", return_value=[])
    @patch("receipt_index.db.get_connection")
    def test_unset_filters_are_none(
        self, mock_conn: MagicMock, mock_search: MagicMock, config_file: Path
    ) -> None:
        result = _invoke(config_file, "search")
        assert result.exit_code == 0, result.output
        kwargs = mock_search.call_args.kwargs
        assert (kwargs["order_number"], kwargs["last4"], kwargs["payer"]) == (
            None,
            None,
            None,
        )

    @pytest.mark.parametrize("bad", ["123", "12345", "12a4", "4111111111111111"])
    @patch("receipt_index.repository.search_receipts")
    @patch("receipt_index.db.get_connection")
    def test_last4_must_be_four_digits(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
        bad: str,
    ) -> None:
        result = _invoke(config_file, "search", "--last4", bad)
        assert result.exit_code == 2
        assert "--last4" in result.output
        mock_search.assert_not_called()

    @pytest.mark.parametrize("option", ["--order-number", "--payer"])
    @patch("receipt_index.repository.search_receipts")
    @patch("receipt_index.db.get_connection")
    def test_blank_text_filter_refused(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,
        option: str,
    ) -> None:
        result = _invoke(config_file, "search", option, "  ")
        assert result.exit_code == 2
        mock_search.assert_not_called()


class TestSearchOutput:
    @patch("receipt_index.repository.search_receipts", return_value=[_RECEIPT, _BARE])
    @patch("receipt_index.db.get_connection")
    def test_text_has_order_and_last4_columns(
        self, mock_conn: MagicMock, mock_search: MagicMock, config_file: Path
    ) -> None:
        result = _invoke(config_file, "search")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert lines[0].split() == [
            "ID",
            "Date",
            "Vendor",
            "Order",
            "Last4",
            "Kind",
            "Amount",
            "Total",
        ]
        first, second = lines[2], lines[3]
        assert "A-100" in first and "1234" in first
        assert first.split()[-2:] == ["30.00", "50.00"]
        assert second.split()[-2:] == ["30.00", "50.00"]

    @patch("receipt_index.repository.search_receipts", return_value=[_RECEIPT])
    @patch("receipt_index.db.get_connection")
    def test_json_has_the_fields(
        self, mock_conn: MagicMock, mock_search: MagicMock, config_file: Path
    ) -> None:
        result = _invoke(config_file, "search", "--output", "json")
        (row,) = json.loads(result.output)["receipts"]
        assert row["order_number"] == "A-100"
        assert row["payment_method"] == "card"
        assert row["payment_last4"] == "1234"
        assert row["payer"] == "Jane Doe"


class TestShowOutput:
    @patch("receipt_index.repository.get_receipt_by_id", return_value=_RECEIPT)
    @patch("receipt_index.db.get_connection")
    def test_order_and_payment_lines(
        self, mock_conn: MagicMock, mock_get: MagicMock, config_file: Path
    ) -> None:
        result = _invoke(config_file, "show", str(_RECEIPT.id))
        assert result.exit_code == 0, result.output
        assert "Order:        A-100" in result.output
        assert "Payment:      card ending 1234 (Jane Doe)" in result.output

    @patch("receipt_index.repository.get_receipt_by_id", return_value=_BARE)
    @patch("receipt_index.db.get_connection")
    def test_no_lines_when_unknown(
        self, mock_conn: MagicMock, mock_get: MagicMock, config_file: Path
    ) -> None:
        result = _invoke(config_file, "show", str(_BARE.id))
        assert result.exit_code == 0, result.output
        assert "Order:" not in result.output
        assert "Payment:" not in result.output


@pytest.mark.parametrize(
    ("columns", "line"),
    [
        ({"payment_method": None, "payment_last4": None}, "Jane Doe"),
        ({"payment_last4": None, "payer": None}, "card"),
        ({"payment_method": None, "payer": None}, "ending 1234"),
        ({"payment_method": None}, "ending 1234 (Jane Doe)"),
    ],
)
@patch("receipt_index.db.get_connection")
def test_partial_payment_line(
    mock_conn: MagicMock, config_file: Path, columns: dict[str, Any], line: str
) -> None:
    receipt = Receipt.model_validate({**_ROW, **columns})
    with patch("receipt_index.repository.get_receipt_by_id", return_value=receipt):
        result = _invoke(config_file, "show", str(receipt.id))
    assert result.exit_code == 0, result.output
    assert f"Payment:      {line}\n" in result.output
