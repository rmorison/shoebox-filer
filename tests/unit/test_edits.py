"""Hand edits: edit_receipt and clear_manual (receipt data-model design, §4).

Each edited field is written, added to ``manual_fields``, loses its
confidence entry, and gets one ``field_edited`` event with the old and new
value, all in one transaction. ``clear_manual`` hands a field back to
re-extraction with a ``manual_cleared`` event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from receipt_index import edits
from receipt_index.edits import EditError, clear_manual, edit_receipt
from receipt_index.models import Receipt, ReceiptEvent

_ID = UUID("019572a0-0000-7000-8000-000000000001")


def _receipt(**overrides: Any) -> Receipt:
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    row: dict[str, Any] = {
        "id": _ID,
        "source_id": "<msg-1@example.com>",
        "source_type": "imap",
        "source_name": "personal-email",
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("50.00"),
        "paid_amount": Decimal("30.00"),
        "currency": "USD",
        "receipt_date": date(2026, 1, 15),
        "description": None,
        "confidence": 0.9,
        "field_confidence": {
            "vendor": 0.9,
            "amount": 0.9,
            "paid_amount": 0.4,
            "date": 0.8,
        },
        "pdf_path": "2026/01/x.pdf",
        "email_subject": None,
        "email_sender": None,
        "email_date": None,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return Receipt.model_validate(row)


@dataclass
class FakeDb:
    """Stands in for the repository calls edits makes."""

    receipt: Receipt | None
    events: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    conn: MagicMock = field(default_factory=MagicMock)

    def lock_receipt(self, conn: Any, receipt_id: UUID) -> Receipt | None:
        return self.receipt

    def insert_event(self, conn: Any, **kwargs: Any) -> ReceiptEvent:
        self.events.append(kwargs)
        return ReceiptEvent(
            id=uuid4(), created_at=datetime(2026, 2, 1, tzinfo=UTC), **kwargs
        )

    def update_edited_fields(
        self,
        conn: Any,
        receipt_id: UUID,
        columns: dict[str, Any],
        *,
        manual_fields: list[str],
        field_confidence: dict[str, float],
    ) -> Receipt:
        self.updates.append(
            {
                "columns": columns,
                "manual_fields": manual_fields,
                "field_confidence": field_confidence,
            }
        )
        assert self.receipt is not None
        return self.receipt.model_copy(
            update={
                **columns,
                "manual_fields": manual_fields,
                "field_confidence": field_confidence,
            }
        )


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    fake = FakeDb(receipt=_receipt())
    for name in ("lock_receipt", "insert_event", "update_edited_fields"):
        monkeypatch.setattr(edits, name, getattr(fake, name))
    return fake


class TestEditReceipt:
    def test_writes_marks_manual_and_records_the_event(self, db: FakeDb) -> None:
        result = edit_receipt(
            db.conn,
            _ID,
            {"paid_amount": Decimal("25.00")},
            actor="operator",
            reason="statement shows 25.00",
        )
        (update,) = db.updates
        assert update["columns"] == {"paid_amount": Decimal("25.00")}
        assert update["manual_fields"] == ["paid_amount"]
        # The confidence entry described the extracted value; it goes.
        assert "paid_amount" not in update["field_confidence"]
        assert update["field_confidence"]["vendor"] == 0.9
        (event,) = db.events
        assert event == {
            "receipt_id": _ID,
            "event_type": "field_edited",
            "payload": {
                "field": "paid_amount",
                "old": "30.00",
                "new": "25.00",
                "old_confidence": 0.4,
            },
            "actor": "operator",
            "actor_kind": "person",
            "reason": "statement shows 25.00",
        }
        db.conn.commit.assert_called_once()
        assert result.receipt.paid_amount == Decimal("25.00")
        assert [c.field for c in result.changes] == ["paid_amount"]

    def test_several_fields_one_event_each(self, db: FakeDb) -> None:
        edit_receipt(
            db.conn,
            _ID,
            {
                "vendor": "Acme Parking",
                "receipt_date": date(2026, 1, 14),
                "order_number": "A-100",
            },
            actor="operator",
            reason=None,
        )
        assert [e["payload"]["field"] for e in db.events] == [
            "vendor",
            "receipt_date",
            "order_number",
        ]
        dated = db.events[1]["payload"]
        assert (dated["old"], dated["new"], dated["old_confidence"]) == (
            "2026-01-15",
            "2026-01-14",
            0.8,
        )
        ordered = db.events[2]["payload"]
        assert (ordered["old"], ordered["old_confidence"]) == (None, None)
        (update,) = db.updates
        assert update["manual_fields"] == ["vendor", "receipt_date", "order_number"]
        assert set(update["field_confidence"]) == {"amount", "paid_amount"}

    def test_same_value_still_pins_the_field(self, db: FakeDb) -> None:
        result = edit_receipt(
            db.conn, _ID, {"paid_amount": Decimal("30.00")}, actor="op", reason=None
        )
        assert db.updates[0]["manual_fields"] == ["paid_amount"]
        assert db.events[0]["payload"]["old"] == db.events[0]["payload"]["new"]
        assert result.changes[0].unchanged

    def test_an_already_manual_field_is_listed_once(self, db: FakeDb) -> None:
        db.receipt = _receipt(manual_fields=["paid_amount"])
        edit_receipt(
            db.conn, _ID, {"paid_amount": Decimal("1.00")}, actor="op", reason=None
        )
        assert db.updates[0]["manual_fields"] == ["paid_amount"]

    def test_unknown_receipt(self, db: FakeDb) -> None:
        db.receipt = None
        with pytest.raises(EditError, match="not found"):
            edit_receipt(
                db.conn, _ID, {"vendor": "Acme Parking"}, actor="op", reason=None
            )
        db.conn.rollback.assert_called_once()
        assert db.events == [] and db.updates == []

    @pytest.mark.parametrize("name", ["amount", "currency", "payer", "kind"])
    def test_only_the_four_fields(self, db: FakeDb, name: str) -> None:
        with pytest.raises(ValueError, match="not an editable field"):
            edit_receipt(db.conn, _ID, {name: "x"}, actor="op", reason=None)
        assert db.events == []

    def test_nothing_to_edit(self, db: FakeDb) -> None:
        with pytest.raises(ValueError, match="nothing to edit"):
            edit_receipt(db.conn, _ID, {}, actor="op", reason=None)


class TestClearManual:
    def test_hands_the_field_back(self, db: FakeDb) -> None:
        db.receipt = _receipt(manual_fields=["vendor", "paid_amount"])
        result = clear_manual(
            db.conn, _ID, ["paid_amount"], actor="operator", reason="re-check"
        )
        (update,) = db.updates
        assert update["columns"] == {}
        assert update["manual_fields"] == ["vendor"]
        (event,) = db.events
        assert event["event_type"] == "manual_cleared"
        assert event["payload"] == {"field": "paid_amount", "value": "30.00"}
        assert event["reason"] == "re-check"
        db.conn.commit.assert_called_once()
        assert result.receipt.manual_fields == ["vendor"]

    def test_date_is_an_alias(self, db: FakeDb) -> None:
        db.receipt = _receipt(manual_fields=["receipt_date"])
        clear_manual(db.conn, _ID, ["date"], actor="op", reason=None)
        assert db.events[0]["payload"]["field"] == "receipt_date"
        assert db.updates[0]["manual_fields"] == []

    def test_a_field_not_hand_edited_is_refused(self, db: FakeDb) -> None:
        db.receipt = _receipt(manual_fields=["vendor"])
        with pytest.raises(EditError, match="paid_amount is not hand-edited"):
            clear_manual(
                db.conn, _ID, ["vendor", "paid_amount"], actor="op", reason=None
            )
        db.conn.rollback.assert_called_once()
        assert db.events == [] and db.updates == []


class TestHandEnteredText:
    def test_vendor_and_reason_are_scrubbed(self, db: FakeDb) -> None:
        edit_receipt(
            db.conn,
            _ID,
            {"vendor": "Acme 4111 1111 1111 1111"},
            actor="op",
            reason="card 4111111111111111 on the statement",
        )
        assert db.updates[0]["columns"] == {"vendor": "Acme ending 1111"}
        assert db.events[0]["payload"]["new"] == "Acme ending 1111"
        assert db.events[0]["reason"] == "card ending 1111 on the statement"

    def test_an_old_vendor_is_scrubbed_in_the_event(self, db: FakeDb) -> None:
        # A receipt extracted before vendor scrubbing may hold a card number.
        db.receipt = _receipt(vendor="Acme 4111 1111 1111 1111")
        edit_receipt(db.conn, _ID, {"vendor": "Acme"}, actor="op", reason=None)
        assert db.events[0]["payload"]["old"] == "Acme ending 1111"
        db.receipt = _receipt(
            vendor="Acme 4111 1111 1111 1111", manual_fields=["vendor"]
        )
        clear_manual(db.conn, _ID, ["vendor"], actor="op", reason=None)
        assert db.events[1]["payload"]["value"] == "Acme ending 1111"

    def test_actor_is_scrubbed(self, db: FakeDb) -> None:
        edit_receipt(
            db.conn, _ID, {"vendor": "Acme"}, actor="op 4111111111111111", reason=None
        )
        db.receipt = _receipt(manual_fields=["vendor"])
        clear_manual(db.conn, _ID, ["vendor"], actor="op 4111111111111111", reason=None)
        assert [e["actor"] for e in db.events] == ["op ending 1111", "op ending 1111"]
