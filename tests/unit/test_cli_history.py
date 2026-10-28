"""``receipt-index history <id>``: a receipt's events, oldest first."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
from uuid import UUID

from receipt_index.models import ReceiptEvent

from . import test_cli_order_payment as _shared
from .test_cli_order_payment import _RECEIPT, _invoke

# The same config-file fixture as the order and payment CLI tests.
config_file = _shared.config_file

if TYPE_CHECKING:
    from pathlib import Path

_EVENTS = [
    ReceiptEvent(
        id=UUID("019572a0-0000-7000-8000-0000000000e1"),
        receipt_id=_RECEIPT.id,
        event_type="field_edited",
        payload={
            "field": "paid_amount",
            "old": "30.00",
            "new": "25.00",
            "old_confidence": 0.4,
        },
        actor="operator",
        actor_kind="person",
        reason="statement shows 25.00",
        created_at=datetime(2026, 2, 1, 9, 0, tzinfo=UTC),
    ),
    ReceiptEvent(
        id=UUID("019572a0-0000-7000-8000-0000000000e2"),
        receipt_id=_RECEIPT.id,
        event_type="field_edited",
        payload={"field": "order_number", "old": None, "new": "A-100"},
        actor="operator",
        actor_kind="person",
        created_at=datetime(2026, 2, 1, 9, 5, tzinfo=UTC),
    ),
    ReceiptEvent(
        id=UUID("019572a0-0000-7000-8000-0000000000e3"),
        receipt_id=_RECEIPT.id,
        event_type="manual_cleared",
        payload={"field": "paid_amount", "value": "25.00"},
        actor="operator",
        actor_kind="person",
        created_at=datetime(2026, 2, 2, 9, 0, tzinfo=UTC),
    ),
]


@patch("receipt_index.repository.get_receipt_events", return_value=_EVENTS)
@patch("receipt_index.repository.get_receipt_by_id", return_value=_RECEIPT)
@patch("receipt_index.db.get_connection")
class TestHistory:
    def test_text_lists_events_in_order(
        self, conn: MagicMock, get: MagicMock, events: MagicMock, config_file: Path
    ) -> None:
        result = _invoke(config_file, "history", str(_RECEIPT.id))
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if "operator" in line]
        assert len(lines) == 3
        assert "field_edited" in lines[0]
        assert "paid_amount 30.00 → 25.00" in lines[0]
        assert "statement shows 25.00" in lines[0]
        assert "operator (person)" in lines[0]
        assert "order_number — → A-100" in lines[1]
        assert "manual_cleared" in lines[2]
        assert "paid_amount handed back to re-extraction" in lines[2]
        assert events.call_args.args[1] == _RECEIPT.id

    def test_json(
        self, conn: MagicMock, get: MagicMock, events: MagicMock, config_file: Path
    ) -> None:
        result = _invoke(config_file, "history", str(_RECEIPT.id), "--output", "json")
        assert result.exit_code == 0, result.output
        data: list[dict[str, Any]] = json.loads(result.output)
        assert [e["event_type"] for e in data] == [
            "field_edited",
            "field_edited",
            "manual_cleared",
        ]
        assert data[0]["payload"]["new"] == "25.00"
        assert data[0]["reason"] == "statement shows 25.00"

    def test_no_events(
        self, conn: MagicMock, get: MagicMock, events: MagicMock, config_file: Path
    ) -> None:
        events.return_value = []
        result = _invoke(config_file, "history", str(_RECEIPT.id))
        assert result.exit_code == 0, result.output
        assert "No history" in result.output

    def test_unknown_receipt(
        self, conn: MagicMock, get: MagicMock, events: MagicMock, config_file: Path
    ) -> None:
        get.return_value = None
        result = _invoke(config_file, "history", str(_RECEIPT.id))
        assert result.exit_code == 1
        assert "Receipt not found" in result.output
        events.assert_not_called()

    def test_invalid_id(
        self, conn: MagicMock, get: MagicMock, events: MagicMock, config_file: Path
    ) -> None:
        result = _invoke(config_file, "history", "not-a-uuid")
        assert result.exit_code == 1
        assert "invalid UUID" in result.output
