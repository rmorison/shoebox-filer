"""Re-extraction's rules for applying a result (data-model design, §3).

``plan_changes`` decides which fields a run writes, given the receipt as it
is now, the new extraction and the receipt's extraction history.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from receipt_index.models import FieldConfidence, Receipt, ReceiptMetadata
from receipt_index.reextract import History, plan_changes

_NOW = datetime(2026, 1, 15, tzinfo=UTC)
_NO_HISTORY = History(has_history=False, applied=frozenset())
_INGESTED = History(
    has_history=True,
    applied=frozenset(
        {
            "vendor",
            "amount",
            "currency",
            "receipt_date",
            "confidence",
            "field_confidence.vendor",
            "field_confidence.amount",
            "field_confidence.paid_amount",
            "field_confidence.date",
        }
    ),
)


def _receipt(**overrides: Any) -> Receipt:
    values: dict[str, Any] = {
        "id": UUID("019572a0-0000-7000-8000-000000000001"),
        "source_id": "<a@example.com>",
        "source_type": "imap",
        "source_name": "personal-email",
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("50.00"),
        "paid_amount": None,
        "currency": "USD",
        "receipt_date": date(2026, 1, 15),
        "description": "Parking",
        "confidence": 0.9,
        "field_confidence": {},
        "manual_fields": [],
        "pdf_path": "2026/01/x.pdf",
        "email_subject": "Receipt",
        "email_sender": "r@example.com",
        "email_date": _NOW,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return Receipt.model_validate(values)


def _meta(**overrides: Any) -> ReceiptMetadata:
    values: dict[str, Any] = {
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("50.00"),
        "paid_amount": Decimal("30.00"),
        "currency": "USD",
        "date": date(2026, 1, 15),
        "description": "Parking",
        "confidence": 0.95,
        "field_confidence": FieldConfidence(
            vendor=0.95, amount=0.9, paid_amount=0.8, date=0.9
        ),
    }
    values.update(overrides)
    return ReceiptMetadata.model_validate(values)


_FC_FULL = {"vendor": 0.9, "amount": 0.9, "paid_amount": 0.5, "date": 0.9}


class TestFillOnly:
    def test_fills_a_null_paid_amount_with_its_confidence(self) -> None:
        receipt = _receipt(field_confidence=_FC_FULL)
        changes = plan_changes(receipt, _meta(), _INGESTED, overwrite=False)
        assert changes["paid_amount"] == (None, Decimal("30.00"))
        # The confidence entry moves with the field it describes.
        assert changes["field_confidence.paid_amount"] == (0.5, 0.8)

    def test_leaves_a_known_paid_amount(self) -> None:
        receipt = _receipt(paid_amount=Decimal("25.00"), field_confidence=_FC_FULL)
        changes = plan_changes(receipt, _meta(), _INGESTED, overwrite=False)
        assert "paid_amount" not in changes
        assert "field_confidence.paid_amount" not in changes

    def test_fills_a_null_description(self) -> None:
        receipt = _receipt(description=None, field_confidence=_FC_FULL)
        changes = plan_changes(receipt, _meta(), _INGESTED, overwrite=False)
        assert changes["description"] == (None, "Parking")

    def test_never_writes_not_null_fields_or_overall_confidence(self) -> None:
        receipt = _receipt(field_confidence=_FC_FULL)
        meta = _meta(
            vendor="Acme Parking",
            currency="CAD",
            date=date(2026, 1, 14),
            confidence=0.6,
        )
        changes = plan_changes(receipt, meta, _INGESTED, overwrite=False)
        assert not {"vendor", "currency", "receipt_date", "confidence"} & set(changes)

    def test_unchanged_values_are_not_listed(self) -> None:
        receipt = _receipt(paid_amount=None, field_confidence=_FC_FULL)
        changes = plan_changes(
            receipt, _meta(paid_amount=None), _INGESTED, overwrite=False
        )
        assert "paid_amount" not in changes

    def test_a_null_result_never_clears_a_value(self) -> None:
        receipt = _receipt(paid_amount=Decimal("25.00"), field_confidence=_FC_FULL)
        changes = plan_changes(
            receipt, _meta(paid_amount=None), _INGESTED, overwrite=True
        )
        assert "paid_amount" not in changes


class TestOldPrompt:
    """No history and an empty field_confidence: extracted under the old prompt."""

    def test_amount_is_replaced_and_its_old_value_kept(self) -> None:
        receipt = _receipt(amount=Decimal("30.00"))  # the payment, not the total
        changes = plan_changes(receipt, _meta(), _NO_HISTORY, overwrite=False)
        assert changes["amount"] == (Decimal("30.00"), Decimal("50.00"))
        assert changes["field_confidence.amount"] == (None, 0.9)

    def test_amount_is_kept_once_there_is_history(self) -> None:
        receipt = _receipt(amount=Decimal("30.00"))
        changes = plan_changes(receipt, _meta(), _INGESTED, overwrite=False)
        assert "amount" not in changes

    def test_amount_is_kept_when_field_confidence_was_filled(self) -> None:
        # Ingested by the paid-amount prompt: no history, but not the old prompt.
        receipt = _receipt(amount=Decimal("30.00"), field_confidence=_FC_FULL)
        changes = plan_changes(receipt, _meta(), _NO_HISTORY, overwrite=False)
        assert "amount" not in changes

    def test_a_confidence_entry_follows_only_its_own_value(self) -> None:
        # Fill-only won't change the vendor, so the new vendor confidence
        # (about a different vendor) isn't written; the equal date's is.
        receipt = _receipt()
        meta = _meta(vendor="Acme Parking")
        changes = plan_changes(receipt, meta, _NO_HISTORY, overwrite=False)
        assert "field_confidence.vendor" not in changes
        assert changes["field_confidence.date"] == (None, 0.9)


class TestManualFields:
    @pytest.mark.parametrize("overwrite", [False, True])
    def test_never_written(self, overwrite: bool) -> None:
        receipt = _receipt(
            paid_amount=None,
            amount=Decimal("30.00"),
            manual_fields=["paid_amount", "amount"],
        )
        changes = plan_changes(receipt, _meta(), _NO_HISTORY, overwrite=overwrite)
        assert not {
            "paid_amount",
            "amount",
            "field_confidence.paid_amount",
            "field_confidence.amount",
        } & set(changes)


class TestOverwrite:
    def test_replaces_fields_an_earlier_run_applied(self) -> None:
        receipt = _receipt(
            vendor="Acme", paid_amount=Decimal("25.00"), field_confidence=_FC_FULL
        )
        history = History(
            has_history=True,
            applied=frozenset({"vendor", "paid_amount", "field_confidence.vendor"}),
        )
        changes = plan_changes(receipt, _meta(), history, overwrite=True)
        assert changes["vendor"] == ("Acme", "Acme Parking (U.S.), LLC")
        assert changes["paid_amount"] == (Decimal("25.00"), Decimal("30.00"))

    def test_leaves_fields_no_earlier_run_applied(self) -> None:
        receipt = _receipt(description="Edited", field_confidence=_FC_FULL)
        history = History(has_history=True, applied=frozenset({"vendor"}))
        changes = plan_changes(receipt, _meta(), history, overwrite=True)
        assert "description" not in changes

    def test_no_history_counts_as_machine_extracted(self) -> None:
        receipt = _receipt(vendor="Acme", receipt_date=date(2026, 1, 1))
        changes = plan_changes(receipt, _meta(), _NO_HISTORY, overwrite=True)
        assert changes["vendor"] == ("Acme", "Acme Parking (U.S.), LLC")
        assert changes["receipt_date"] == (date(2026, 1, 1), date(2026, 1, 15))

    def test_replaces_overall_confidence_when_it_applies_anything(self) -> None:
        receipt = _receipt(vendor="Acme", confidence=0.7)
        changes = plan_changes(receipt, _meta(), _NO_HISTORY, overwrite=True)
        assert changes["confidence"] == (pytest.approx(0.7), 0.95)

    def test_leaves_overall_confidence_when_nothing_else_changes(self) -> None:
        receipt = _receipt(
            paid_amount=Decimal("30.00"),
            confidence=0.7,
            field_confidence={
                "vendor": 0.95,
                "amount": 0.9,
                "paid_amount": 0.8,
                "date": 0.9,
            },
        )
        changes = plan_changes(receipt, _meta(), _INGESTED, overwrite=True)
        assert changes == {}
