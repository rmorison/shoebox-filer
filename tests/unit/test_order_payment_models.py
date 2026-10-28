"""Order number and payment on the extraction output and the receipt model.

The receipt data-model design, §2 and §4. Only the last four digits of a
card or account number are ever kept: a longer number from the extractor is
cut to its last four, and never fails validation (which would lose the
whole receipt and echo the number into a logged error).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from receipt_index.models import (
    FieldConfidence,
    Payment,
    Receipt,
    ReceiptMetadata,
    receipt_values,
)

# A well-known test card number, not a real account.
_TEST_CARD = "4111 1111 1111 1111"


def _metadata(**overrides: Any) -> ReceiptMetadata:
    values: dict[str, Any] = {
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": "50.00",
        "date": "2026-01-15",
        "confidence": 0.9,
        "field_confidence": {
            "vendor": 0.9,
            "amount": 0.9,
            "paid_amount": 0.9,
            "date": 0.9,
        },
    }
    values.update(overrides)
    return ReceiptMetadata.model_validate(values)


class TestLast4:
    @pytest.mark.parametrize(
        ("raw", "kept"),
        [
            ("1234", "1234"),
            (_TEST_CARD, "1111"),
            ("4111-1111-1111-4321", "4321"),
            ("**** **** **** 9876", "9876"),
            ("ending in 5555", "5555"),
            ("acct 000123456789", "6789"),
            # A JSON number: never handed to the pattern check, whose error
            # would echo the whole value into a logged traceback.
            (4111111111114444, "4444"),
        ],
    )
    def test_keeps_only_the_last_four_digits(self, raw: Any, kept: str) -> None:
        assert Payment.model_validate({"last4": raw}).last4 == kept

    @pytest.mark.parametrize("raw", ["123", "", "n/a", "ending ••", None, 12, [1]])
    def test_fewer_than_four_digits_is_none(self, raw: Any) -> None:
        assert Payment.model_validate({"last4": raw, "method": "card"}).last4 is None

    def test_full_number_never_reaches_the_dump(self) -> None:
        meta = _metadata(payment={"method": "card", "last4": _TEST_CARD})
        dumped = meta.model_dump_json()
        assert "4111" not in dumped
        assert meta.payment is not None
        assert meta.payment.last4 == "1111"


class TestPayment:
    def test_defaults_to_none(self) -> None:
        meta = _metadata()
        assert meta.payment is None
        assert meta.order_number is None

    def test_all_fields(self) -> None:
        meta = _metadata(
            order_number=" A-100 ",
            payment={"method": "card", "last4": "1234", "payer": " Jane Doe "},
        )
        assert meta.order_number == "A-100"
        assert meta.payment == Payment(method="card", last4="1234", payer="Jane Doe")

    def test_an_empty_payment_is_none(self) -> None:
        meta = _metadata(payment={"method": None, "last4": "n/a", "payer": " "})
        assert meta.payment is None

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_order_number_is_none(self, blank: str) -> None:
        assert _metadata(order_number=blank).order_number is None

    def test_unknown_method_rejected(self) -> None:
        with pytest.raises(ValidationError, match="method"):
            Payment(method="cheque")  # type: ignore[arg-type]


class TestFieldConfidence:
    def test_new_entries_are_optional(self) -> None:
        fc = FieldConfidence(vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9)
        assert fc.order_number is None
        assert fc.payment is None
        assert fc.model_dump(exclude_none=True) == {
            "vendor": 0.9,
            "amount": 0.9,
            "paid_amount": 0.9,
            "date": 0.9,
        }

    @pytest.mark.parametrize("field", ["order_number", "payment"])
    @pytest.mark.parametrize("value", [-0.1, 1.1])
    def test_out_of_range_rejected(self, field: str, value: float) -> None:
        values: dict[str, Any] = {
            "vendor": 0.9,
            "amount": 0.9,
            "paid_amount": 0.9,
            "date": 0.9,
            field: value,
        }
        with pytest.raises(ValidationError, match=field):
            FieldConfidence(**values)


class TestReceiptValues:
    def test_payment_spreads_over_its_columns(self) -> None:
        meta = _metadata(
            order_number="A-100",
            payment={"method": "card", "last4": "1234", "payer": "Jane Doe"},
            field_confidence={
                "vendor": 0.9,
                "amount": 0.9,
                "paid_amount": 0.9,
                "date": 0.9,
                "order_number": 0.8,
                "payment": 0.7,
            },
        )
        values = receipt_values(meta)
        assert values["order_number"] == "A-100"
        assert values["payment_method"] == "card"
        assert values["payment_last4"] == "1234"
        assert values["payer"] == "Jane Doe"
        assert values["field_confidence.order_number"] == 0.8
        assert values["field_confidence.payment"] == 0.7

    def test_no_payment_gives_nulls(self) -> None:
        values = receipt_values(_metadata())
        for name in ("order_number", "payment_method", "payment_last4", "payer"):
            assert values[name] is None
        assert values["field_confidence.payment"] is None


class TestReceipt:
    def test_new_columns_default_to_none(self) -> None:
        now = datetime(2026, 1, 15, tzinfo=UTC)
        receipt = Receipt.model_validate(
            {
                "id": uuid4(),
                "source_id": "msg-1",
                "source_type": "imap",
                "source_name": "personal-email",
                "vendor": "Acme Parking (U.S.), LLC",
                "amount": Decimal("12.00"),
                "currency": "USD",
                "receipt_date": date(2026, 1, 15),
                "description": None,
                "confidence": 0.9,
                "pdf_path": "2026/01/x.pdf",
                "email_subject": None,
                "email_sender": None,
                "email_date": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        data = receipt.model_dump(mode="json")
        for name in ("order_number", "payment_method", "payment_last4", "payer"):
            assert data[name] is None
