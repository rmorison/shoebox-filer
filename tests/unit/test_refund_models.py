"""Kind and signed amount on the extraction output and the receipt model.

The receipt data-model design, §6, widened by the operator's decision on
the refunds request: ``kind`` is purchase, refund, credit or statement, and
``signed_amount`` makes a plain sum the net spend (a refund or credit counts
against it, a statement counts nothing).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from receipt_index.extraction import (
    _SYSTEM_PROMPT,
    DOCUMENT_SYSTEM_PROMPT,
    EXTRACTION_PROMPT_VERSION,
)
from receipt_index.models import Receipt, ReceiptMetadata, receipt_values


def _metadata(**overrides: Any) -> ReceiptMetadata:
    values: dict[str, Any] = {
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": "25.00",
        "paid_amount": "25.00",
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


def _receipt(**overrides: Any) -> Receipt:
    values: dict[str, Any] = {
        "id": uuid4(),
        "source_id": "<r@example.com>",
        "source_type": "imap",
        "source_name": "personal-email",
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("25.00"),
        "paid_amount": Decimal("25.00"),
        "currency": "USD",
        "receipt_date": date(2026, 1, 15),
        "description": None,
        "confidence": 0.9,
        "pdf_path": "2026/01/x.pdf",
        "email_subject": None,
        "email_sender": None,
        "email_date": None,
        "created_at": datetime(2026, 1, 15, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 15, tzinfo=UTC),
    }
    values.update(overrides)
    return Receipt.model_validate(values)


class TestKind:
    def test_default_is_purchase(self) -> None:
        assert _metadata().kind == "purchase"

    @pytest.mark.parametrize("kind", ["purchase", "refund", "credit", "statement"])
    def test_the_four_kinds(self, kind: str) -> None:
        assert _metadata(kind=kind).kind == kind

    @pytest.mark.parametrize("kind", ["other", "charge", "Refund", ""])
    def test_anything_else_is_refused(self, kind: str) -> None:
        with pytest.raises(ValidationError):
            _metadata(kind=kind)

    def test_kind_confidence_is_optional(self) -> None:
        assert _metadata().field_confidence.kind is None
        fc = {
            "vendor": 0.9,
            "amount": 0.9,
            "paid_amount": 0.9,
            "date": 0.9,
            "kind": 0.4,
        }
        assert _metadata(field_confidence=fc).field_confidence.kind == 0.4

    def test_receipt_values_carry_kind(self) -> None:
        values = receipt_values(_metadata(kind="refund"))
        assert values["kind"] == "refund"
        assert "field_confidence.kind" in values


class TestSignedAmount:
    @pytest.mark.parametrize(
        ("kind", "signed"),
        [
            ("purchase", Decimal("25.00")),
            ("refund", Decimal("-25.00")),
            ("credit", Decimal("-25.00")),
            ("statement", Decimal("0")),
        ],
    )
    def test_by_kind(self, kind: str, signed: Decimal) -> None:
        assert _receipt(kind=kind).signed_amount == signed

    def test_falls_back_to_the_total(self) -> None:
        r = _receipt(kind="refund", paid_amount=None, amount=Decimal("12.50"))
        assert r.signed_amount == Decimal("-12.50")

    def test_store_credit_refund_is_zero_not_negative_zero(self) -> None:
        r = _receipt(kind="refund", paid_amount=Decimal("0.00"))
        assert r.signed_amount == 0
        assert str(r.signed_amount) == "0.00"

    def test_a_purchase_and_its_refund_net_to_zero(self) -> None:
        receipts = [_receipt(), _receipt(kind="refund")]
        assert sum(r.signed_amount for r in receipts) == 0

    def test_json_output_has_kind_and_signed_amount(self) -> None:
        data = _receipt(kind="refund").model_dump(mode="json")
        assert data["kind"] == "refund"
        assert data["signed_amount"] == "-25.00"
        assert data["effective_amount"] == "25.00"
        assert data["refund_of"] is None

    def test_existing_rows_are_purchases(self) -> None:
        r = _receipt()
        assert r.kind == "purchase"
        assert r.signed_amount == r.effective_amount


class TestKindPrompt:
    """Both prompts carry the kind rules. A guard against an edit dropping one."""

    _RULES = (
        "- kind: one of purchase, refund, credit or statement",
        "- purchase:",
        "- refund:",
        "- credit:",
        "- statement:",
        "For a refund or credit, the original order or invoice number",
        # The operator's ruling: a statement that bills its own period is the
        # bill, and counts; only a summary of transactions charged or
        # receipted elsewhere counts 0.
        "A statement that bills a new charge for its own period (a utility, "
        "phone or subscription statement) is a purchase.",
        "card and bank statements, and vendor statements that summarise "
        "orders already receipted",
    )

    @pytest.mark.parametrize("prompt", [_SYSTEM_PROMPT, DOCUMENT_SYSTEM_PROMPT])
    @pytest.mark.parametrize("rule", _RULES)
    def test_rule_present(self, prompt: str, rule: str) -> None:
        assert rule in " ".join(prompt.split())

    @pytest.mark.parametrize("prompt", [_SYSTEM_PROMPT, DOCUMENT_SYSTEM_PROMPT])
    def test_kind_has_a_confidence(self, prompt: str) -> None:
        assert "order_number, payment and kind" in " ".join(prompt.split())

    def test_prompt_version(self) -> None:
        # 6 added kind (and the refund's original order number).
        assert EXTRACTION_PROMPT_VERSION == 6
