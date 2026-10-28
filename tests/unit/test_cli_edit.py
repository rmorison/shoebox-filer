"""``receipt-index edit``: CLI parsing, the actor, and the output.

The edit logic itself is tested in test_edits.py; here edit_receipt and
clear_manual are mocked, to check what the command passes them.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from click.testing import CliRunner

from receipt_index.cli import cli
from receipt_index.config import AppConfig
from receipt_index.edits import EditError, EditResult, FieldChange
from receipt_index.models import ReceiptEvent

from . import test_cli_order_payment as _shared
from .test_cli_order_payment import _RECEIPT

if TYPE_CHECKING:
    from pathlib import Path

config_file = _shared.config_file
_ID = str(_RECEIPT.id)


def _event(event_type: str, payload: dict[str, Any]) -> ReceiptEvent:
    return ReceiptEvent(
        id=uuid4(),
        receipt_id=_RECEIPT.id,
        event_type=event_type,  # type: ignore[arg-type]
        payload=payload,
        actor="operator",
        actor_kind="person",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _result(*changes: FieldChange) -> EditResult:
    return EditResult(
        _RECEIPT,
        list(changes),
        [
            _event("field_edited", {"field": c.field, "old": None, "new": None})
            for c in changes
        ],
    )


def _invoke(config_file: Path, *args: str, config: AppConfig | None = None) -> Any:
    with patch(
        "receipt_index.cli.load_config", return_value=config or _shared._config()
    ):
        return CliRunner().invoke(cli, ["--config", str(config_file), *args])


@pytest.fixture
def edit() -> Any:
    with (
        patch("receipt_index.db.get_connection"),
        patch(
            "receipt_index.edits.edit_receipt",
            return_value=_result(
                FieldChange("paid_amount", Decimal("30.00"), Decimal("25.00"))
            ),
        ) as mock,
    ):
        yield mock


@pytest.fixture
def clear() -> Any:
    with (
        patch("receipt_index.db.get_connection"),
        patch(
            "receipt_index.edits.clear_manual",
            return_value=EditResult(
                _RECEIPT,
                [],
                [_event("manual_cleared", {"field": "paid_amount", "value": "25"})],
            ),
        ) as mock,
    ):
        yield mock


class TestEditValues:
    def test_every_field(self, edit: MagicMock, config_file: Path) -> None:
        result = _invoke(
            config_file,
            "edit",
            _ID,
            "--paid-amount",
            "25.00",
            "--vendor",
            " Acme Parking ",
            "--date",
            "2026-01-14",
            "--order-number",
            "A-100",
            "--reason",
            "statement shows 25.00",
        )
        assert result.exit_code == 0, result.output
        args, kwargs = edit.call_args
        assert args[1] == _RECEIPT.id
        assert args[2] == {
            "paid_amount": Decimal("25.00"),
            "vendor": "Acme Parking",
            "receipt_date": date(2026, 1, 14),
            "order_number": "A-100",
        }
        assert kwargs == {"actor": "operator", "reason": "statement shows 25.00"}
        assert "paid_amount: 30.00 → 25.00" in result.output

    @pytest.mark.parametrize(
        "bad",
        ["-1", "1.234", "abc", "NaN", "Infinity", "1e15", "10000000000", "9" * 40],
    )
    def test_bad_paid_amount(
        self, edit: MagicMock, config_file: Path, bad: str
    ) -> None:
        result = _invoke(config_file, "edit", _ID, "--paid-amount", bad)
        assert result.exit_code == 2
        assert "--paid-amount" in result.output
        edit.assert_not_called()

    @pytest.mark.parametrize("option", ["--vendor", "--order-number"])
    def test_blank_text_refused(
        self, edit: MagicMock, config_file: Path, option: str
    ) -> None:
        result = _invoke(config_file, "edit", _ID, option, "  ")
        assert result.exit_code == 2
        edit.assert_not_called()

    def test_nothing_to_do(self, edit: MagicMock, config_file: Path) -> None:
        result = _invoke(config_file, "edit", _ID, "--reason", "why")
        assert result.exit_code == 2
        assert "nothing to edit" in result.output
        edit.assert_not_called()

    def test_unchanged_value_is_reported(
        self, edit: MagicMock, config_file: Path
    ) -> None:
        edit.return_value = _result(
            FieldChange("paid_amount", Decimal("30.00"), Decimal("30.00"))
        )
        result = _invoke(config_file, "edit", _ID, "--paid-amount", "30.00")
        assert result.exit_code == 0, result.output
        assert "paid_amount: 30.00 (unchanged; now hand-set)" in result.output

    def test_json(self, edit: MagicMock, config_file: Path) -> None:
        result = _invoke(
            config_file, "edit", _ID, "--paid-amount", "25", "--output", "json"
        )
        data = json.loads(result.output)
        assert data["receipt"]["id"] == _ID
        assert data["events"][0]["event_type"] == "field_edited"

    def test_edit_error_exits_1(self, edit: MagicMock, config_file: Path) -> None:
        edit.side_effect = EditError(f"Receipt not found: {_ID}")
        result = _invoke(config_file, "edit", _ID, "--vendor", "Acme Parking")
        assert result.exit_code == 1
        assert "Receipt not found" in result.output

    def test_invalid_id(self, edit: MagicMock, config_file: Path) -> None:
        result = _invoke(config_file, "edit", "nope", "--vendor", "Acme Parking")
        assert result.exit_code == 1
        edit.assert_not_called()


class TestActor:
    def test_defaults_to_operator(self, edit: MagicMock, config_file: Path) -> None:
        _invoke(config_file, "edit", _ID, "--vendor", "Acme Parking")
        assert edit.call_args.kwargs["actor"] == "operator"

    def test_from_the_configuration(self, edit: MagicMock, config_file: Path) -> None:
        config = _shared._config().model_copy(
            update={
                "annotations": _shared._config().annotations.model_copy(
                    update={"actor": "bookkeeper"}
                )
            }
        )
        _invoke(config_file, "edit", _ID, "--vendor", "Acme Parking", config=config)
        assert edit.call_args.kwargs["actor"] == "bookkeeper"

    def test_as_overrides(self, edit: MagicMock, config_file: Path) -> None:
        _invoke(config_file, "edit", _ID, "--vendor", "Acme Parking", "--as", " Pat ")
        assert edit.call_args.kwargs["actor"] == "Pat"

    def test_blank_as_refused(self, edit: MagicMock, config_file: Path) -> None:
        result = _invoke(config_file, "edit", _ID, "--vendor", "x", "--as", " ")
        assert result.exit_code == 2
        edit.assert_not_called()

    def test_config_key_parses(self) -> None:
        config = AppConfig.model_validate(
            {
                "sources": [],
                "database": {"url": "postgresql://localhost/test"},
                "llm": {"api_key": "k"},  # pragma: allowlist secret
                "annotations": {"actor": " bookkeeper "},
            }
        )
        assert config.annotations.actor == "bookkeeper"


class TestClearManual:
    def test_clears(self, clear: MagicMock, config_file: Path) -> None:
        result = _invoke(
            config_file,
            "edit",
            _ID,
            "--clear-manual",
            "paid_amount",
            "--clear-manual",
            "date",
            "--reason",
            "re-check",
        )
        assert result.exit_code == 0, result.output
        args, kwargs = clear.call_args
        assert args[1] == _RECEIPT.id
        assert list(args[2]) == ["paid_amount", "date"]
        assert kwargs == {"actor": "operator", "reason": "re-check"}
        assert "paid_amount handed back to re-extraction" in result.output

    def test_unknown_field(self, clear: MagicMock, config_file: Path) -> None:
        result = _invoke(config_file, "edit", _ID, "--clear-manual", "amount")
        assert result.exit_code == 2
        clear.assert_not_called()

    def test_not_with_setters(self, clear: MagicMock, config_file: Path) -> None:
        result = _invoke(
            config_file, "edit", _ID, "--clear-manual", "vendor", "--vendor", "x"
        )
        assert result.exit_code == 2
        assert "--clear-manual" in result.output
        clear.assert_not_called()

    def test_not_hand_edited_exits_1(self, clear: MagicMock, config_file: Path) -> None:
        clear.side_effect = EditError("vendor is not hand-edited")
        result = _invoke(config_file, "edit", _ID, "--clear-manual", "vendor")
        assert result.exit_code == 1
        assert "vendor is not hand-edited" in result.output
