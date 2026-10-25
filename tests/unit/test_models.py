"""Tests for receipt_index.models."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from receipt_index.models import Receipt, ReceiptMetadata


class TestReceiptMetadata:
    """Tests for ReceiptMetadata validation."""

    def test_valid_metadata(self) -> None:
        meta = ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("42.99"),
            date=date(2025, 6, 15),
            confidence=0.95,
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
            )

    def test_zero_amount_accepted(self) -> None:
        meta = ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("0"),
            date=date(2025, 6, 15),
            confidence=0.9,
        )
        assert meta.amount == Decimal("0")

    def test_empty_vendor_rejected(self) -> None:
        with pytest.raises(ValidationError, match="vendor"):
            ReceiptMetadata(
                vendor="",
                amount=Decimal("10.00"),
                date=date(2025, 6, 15),
                confidence=0.9,
            )

    def test_missing_vendor_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReceiptMetadata(  # type: ignore[call-arg]
                amount=Decimal("10.00"),
                date=date(2025, 6, 15),
                confidence=0.9,
            )

    def test_valid_currency_codes(self) -> None:
        for code in ("USD", "CAD", "EUR", "GBP"):
            meta = ReceiptMetadata(
                vendor="Test",
                amount=Decimal("10.00"),
                date=date(2025, 1, 1),
                confidence=0.9,
                currency=code,
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
                )

    def test_confidence_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="confidence"):
            ReceiptMetadata(
                vendor="Amazon",
                amount=Decimal("10.00"),
                date=date(2025, 6, 15),
                confidence=-0.1,
            )

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError, match="confidence"):
            ReceiptMetadata(
                vendor="Amazon",
                amount=Decimal("10.00"),
                date=date(2025, 6, 15),
                confidence=1.1,
            )

    def test_confidence_boundary_zero(self) -> None:
        meta = ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("10.00"),
            date=date(2025, 6, 15),
            confidence=0.0,
        )
        assert meta.confidence == 0.0

    def test_confidence_boundary_one(self) -> None:
        meta = ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("10.00"),
            date=date(2025, 6, 15),
            confidence=1.0,
        )
        assert meta.confidence == 1.0

    def test_small_amount(self) -> None:
        meta = ReceiptMetadata(
            vendor="Vending Machine",
            amount=Decimal("0.50"),
            date=date(2025, 1, 1),
            confidence=0.7,
        )
        assert meta.amount == Decimal("0.50")

    def test_large_amount(self) -> None:
        meta = ReceiptMetadata(
            vendor="Contractor",
            amount=Decimal("9999999999.99"),
            date=date(2025, 1, 1),
            confidence=0.85,
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
