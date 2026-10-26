"""Tests for receipt_index.models."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from receipt_index.models import (
    FieldConfidence,
    IngestLogEntry,
    Receipt,
    ReceiptMetadata,
)


class TestReceiptMetadata:
    """Tests for ReceiptMetadata validation."""

    def test_valid_metadata(self) -> None:
        meta = ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("42.99"),
            date=date(2025, 6, 15),
            confidence=0.95,
            field_confidence=_field_confidence(),
        )
        assert meta.vendor == "Amazon"
        assert meta.amount == Decimal("42.99")
        assert meta.currency == "USD"
        assert meta.date == date(2025, 6, 15)
        assert meta.description is None
        assert meta.confidence == 0.95

    def test_with_all_fields(self) -> None:
        meta = ReceiptMetadata(
            vendor="Home Depot",
            amount=Decimal("1250.00"),
            currency="CAD",
            date=date(2025, 3, 1),
            description="Lumber and supplies",
            confidence=0.88,
            field_confidence=_field_confidence(),
        )
        assert meta.currency == "CAD"
        assert meta.description == "Lumber and supplies"

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(ValidationError, match="amount"):
            ReceiptMetadata(
                vendor="Amazon",
                amount=Decimal("-10.00"),
                date=date(2025, 6, 15),
                confidence=0.9,
                field_confidence=_field_confidence(),
            )

    def test_zero_amount_accepted(self) -> None:
        meta = ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("0"),
            date=date(2025, 6, 15),
            confidence=0.9,
            field_confidence=_field_confidence(),
        )
        assert meta.amount == Decimal("0")

    def test_empty_vendor_rejected(self) -> None:
        with pytest.raises(ValidationError, match="vendor"):
            ReceiptMetadata(
                vendor="",
                amount=Decimal("10.00"),
                date=date(2025, 6, 15),
                confidence=0.9,
                field_confidence=_field_confidence(),
            )

    def test_missing_vendor_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReceiptMetadata(  # type: ignore[call-arg]
                amount=Decimal("10.00"),
                date=date(2025, 6, 15),
                confidence=0.9,
                field_confidence=_field_confidence(),
            )

    def test_valid_currency_codes(self) -> None:
        for code in ("USD", "CAD", "EUR", "GBP"):
            meta = ReceiptMetadata(
                vendor="Test",
                amount=Decimal("10.00"),
                date=date(2025, 1, 1),
                confidence=0.9,
                currency=code,
                field_confidence=_field_confidence(),
            )
            assert meta.currency == code

    def test_invalid_currency_rejected(self) -> None:
        for bad in ("usd", "us", "US Dollars", "1234", ""):
            with pytest.raises(ValidationError, match="currency"):
                ReceiptMetadata(
                    vendor="Test",
                    amount=Decimal("10.00"),
                    date=date(2025, 1, 1),
                    confidence=0.9,
                    currency=bad,
                    field_confidence=_field_confidence(),
                )

    def test_confidence_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="confidence"):
            ReceiptMetadata(
                vendor="Amazon",
                amount=Decimal("10.00"),
                date=date(2025, 6, 15),
                confidence=-0.1,
                field_confidence=_field_confidence(),
            )

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError, match="confidence"):
            ReceiptMetadata(
                vendor="Amazon",
                amount=Decimal("10.00"),
                date=date(2025, 6, 15),
                confidence=1.1,
                field_confidence=_field_confidence(),
            )

    def test_confidence_boundary_zero(self) -> None:
        meta = ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("10.00"),
            date=date(2025, 6, 15),
            confidence=0.0,
            field_confidence=_field_confidence(),
        )
        assert meta.confidence == 0.0

    def test_confidence_boundary_one(self) -> None:
        meta = ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("10.00"),
            date=date(2025, 6, 15),
            confidence=1.0,
            field_confidence=_field_confidence(),
        )
        assert meta.confidence == 1.0

    def test_small_amount(self) -> None:
        meta = ReceiptMetadata(
            vendor="Vending Machine",
            amount=Decimal("0.50"),
            date=date(2025, 1, 1),
            confidence=0.7,
            field_confidence=_field_confidence(),
        )
        assert meta.amount == Decimal("0.50")

    def test_large_amount(self) -> None:
        meta = ReceiptMetadata(
            vendor="Contractor",
            amount=Decimal("9999999999.99"),
            date=date(2025, 1, 1),
            confidence=0.85,
            field_confidence=_field_confidence(),
        )
        assert meta.amount == Decimal("9999999999.99")


class TestReceipt:
    """Tests for Receipt model serialization."""

    def test_receipt_serialization(self) -> None:
        now = datetime.now(tz=UTC)
        receipt = Receipt(
            id=uuid4(),
            source_id="msg-123",
            source_type="imap",
            source_name="test-email",
            vendor="Amazon",
            amount=Decimal("42.99"),
            currency="USD",
            receipt_date=date(2025, 6, 15),
            description="Books",
            confidence=0.95,
            pdf_path="2025/06/2025-06-15__amazon__42.99.pdf",
            email_subject="Your order",
            email_sender="no-reply@amazon.com",
            email_date=now,
            created_at=now,
            updated_at=now,
        )
        data = receipt.model_dump()
        assert data["vendor"] == "Amazon"
        assert data["amount"] == Decimal("42.99")
        assert data["pdf_path"] == "2025/06/2025-06-15__amazon__42.99.pdf"

    def test_receipt_json_roundtrip(self) -> None:
        now = datetime.now(tz=UTC)
        receipt = Receipt(
            id=uuid4(),
            source_id="msg-456",
            source_type="imap",
            source_name="test-email",
            vendor="Costco",
            amount=Decimal("157.32"),
            currency="USD",
            receipt_date=date(2025, 3, 10),
            description=None,
            confidence=0.88,
            pdf_path="2025/03/2025-03-10__costco__157.32.pdf",
            email_subject=None,
            email_sender=None,
            email_date=None,
            created_at=now,
            updated_at=now,
        )
        json_str = receipt.model_dump_json()
        restored = Receipt.model_validate_json(json_str)
        assert restored.vendor == receipt.vendor
        assert restored.amount == receipt.amount
        assert restored.receipt_date == receipt.receipt_date


def _field_confidence(**overrides: float) -> FieldConfidence:
    values = {"vendor": 0.9, "amount": 0.9, "paid_amount": 0.9, "date": 0.9}
    values.update(overrides)
    return FieldConfidence(**values)


def _metadata(**overrides: object) -> ReceiptMetadata:
    values: dict[str, object] = {
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("50.00"),
        "date": date(2025, 6, 15),
        "confidence": 0.9,
        "field_confidence": _field_confidence(),
    }
    values.update(overrides)
    return ReceiptMetadata.model_validate(values)


class TestPaidAmountMetadata:
    """paid_amount and field_confidence on the extraction output model."""

    def test_paid_amount_defaults_to_unknown(self) -> None:
        assert _metadata().paid_amount is None

    def test_paid_amount_zero_accepted(self) -> None:
        assert _metadata(paid_amount=Decimal("0")).paid_amount == Decimal("0")

    def test_paid_amount_below_total_accepted(self) -> None:
        meta = _metadata(paid_amount=Decimal("30.00"))
        assert meta.paid_amount == Decimal("30.00")
        assert meta.amount == Decimal("50.00")

    def test_negative_paid_amount_rejected(self) -> None:
        with pytest.raises(ValidationError, match="paid_amount"):
            _metadata(paid_amount=Decimal("-0.01"))

    def test_field_confidence_required(self) -> None:
        with pytest.raises(ValidationError, match="field_confidence"):
            ReceiptMetadata(  # type: ignore[call-arg]
                vendor="Acme Parking (U.S.), LLC",
                amount=Decimal("50.00"),
                date=date(2025, 6, 15),
                confidence=0.9,
            )

    @pytest.mark.parametrize("field", ["vendor", "amount", "paid_amount", "date"])
    @pytest.mark.parametrize("value", [-0.1, 1.1])
    def test_field_confidence_out_of_range_rejected(
        self, field: str, value: float
    ) -> None:
        with pytest.raises(ValidationError, match=field):
            _field_confidence(**{field: value})

    @pytest.mark.parametrize("field", ["vendor", "amount", "paid_amount", "date"])
    def test_field_confidence_each_entry_required(self, field: str) -> None:
        values = {"vendor": 0.9, "amount": 0.9, "paid_amount": 0.9, "date": 0.9}
        del values[field]
        with pytest.raises(ValidationError, match=field):
            FieldConfidence(**values)


def _receipt(**overrides: object) -> Receipt:
    now = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "source_id": "msg-789",
        "source_type": "imap",
        "source_name": "test-email",
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("50.00"),
        "currency": "USD",
        "receipt_date": date(2025, 6, 15),
        "description": None,
        "confidence": 0.9,
        "pdf_path": "2025/06/x.pdf",
        "email_subject": None,
        "email_sender": None,
        "email_date": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return Receipt.model_validate(values)


class TestEffectiveAmount:
    """Receipt.effective_amount: paid when known, the total otherwise."""

    def test_row_without_new_columns_validates(self) -> None:
        receipt = _receipt()
        assert receipt.paid_amount is None
        assert receipt.field_confidence == {}

    def test_paid_amount_known(self) -> None:
        receipt = _receipt(paid_amount=Decimal("30.00"))
        assert receipt.effective_amount == Decimal("30.00")
        assert receipt.amount == Decimal("50.00")

    def test_paid_amount_unknown_falls_back_to_total(self) -> None:
        assert _receipt().effective_amount == Decimal("50.00")

    def test_paid_amount_zero_does_not_fall_back(self) -> None:
        # An unpaid invoice: 0 is a known value, not "unknown".
        assert _receipt(paid_amount=Decimal("0")).effective_amount == Decimal("0")

    def test_json_dump_keeps_amount_and_adds_both(self) -> None:
        data = _receipt(
            paid_amount=Decimal("30.00"),
            field_confidence={"vendor": 0.9, "paid_amount": 0.6},
        ).model_dump(mode="json")
        assert data["amount"] == "50.00"
        assert data["paid_amount"] == "30.00"
        assert data["effective_amount"] == "30.00"
        assert data["field_confidence"] == {"vendor": 0.9, "paid_amount": 0.6}

    def test_json_roundtrip_with_effective_amount(self) -> None:
        receipt = _receipt(paid_amount=Decimal("0"))
        restored = Receipt.model_validate_json(receipt.model_dump_json())
        assert restored.paid_amount == Decimal("0")
        assert restored.effective_amount == Decimal("0")


class TestIngestLogPaidAmount:
    def test_paid_amount_defaults_to_none(self) -> None:
        entry = IngestLogEntry.model_validate(
            {
                "id": uuid4(),
                "source_id": "msg-1",
                "source_type": "imap",
                "status": "success",
                "receipt_id": None,
                "vendor": None,
                "amount": None,
                "email_subject": None,
                "email_sender": None,
                "email_date": None,
                "error_message": None,
                "created_at": datetime(2025, 6, 15, tzinfo=UTC),
            }
        )
        assert entry.paid_amount is None
