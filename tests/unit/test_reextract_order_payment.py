"""Re-extraction of the order number and payment (data-model design, §3, §4).

One ``payment`` confidence covers three columns, so it moves with them as a
group: it is written when every column holds the extracted value afterwards,
and removed when a run changes the group without leaving it matching, so it
never describes values the receipt doesn't hold. A field whose hand edit was
cleared counts as machine-extracted again.
"""

from __future__ import annotations

from typing import Any

from receipt_index.models import FieldConfidence
from receipt_index.reextract import History, plan_changes

from .test_reextract_rules import _INGESTED, _NO_HISTORY, _meta, _receipt

_FC = {"vendor": 0.95, "amount": 0.9, "paid_amount": 0.8, "date": 0.9}


def _paid_meta(**overrides: Any) -> Any:
    fc = FieldConfidence(**_FC, order_number=0.9, payment=0.7)
    return _meta(field_confidence=fc, **overrides)


_PAYMENT = {"method": "card", "last4": "1234", "payer": "Jane Doe"}
_PAYMENT_COLUMNS = {
    "payment_method": "card",
    "payment_last4": "1234",
    "payer": "Jane Doe",
}


class TestFillOnly:
    def test_fills_order_number_and_payment_with_confidences(self) -> None:
        changes = plan_changes(
            _receipt(),
            _paid_meta(order_number="A-100", payment=_PAYMENT),
            _INGESTED,
            overwrite=False,
        )
        assert changes["order_number"] == (None, "A-100")
        assert changes["payment_method"] == (None, "card")
        assert changes["payment_last4"] == (None, "1234")
        assert changes["payer"] == (None, "Jane Doe")
        assert changes["field_confidence.order_number"] == (None, 0.9)
        assert changes["field_confidence.payment"] == (None, 0.7)

    def test_fills_each_payment_column_on_its_own(self) -> None:
        # The stored payer differs, so the group won't match afterwards: the
        # other columns fill, and no payment confidence is written.
        receipt = _receipt(payer="Someone Else")
        changes = plan_changes(
            receipt, _paid_meta(payment=_PAYMENT), _INGESTED, overwrite=False
        )
        assert changes["payment_method"] == (None, "card")
        assert changes["payment_last4"] == (None, "1234")
        assert "payer" not in changes
        assert "field_confidence.payment" not in changes

    def test_payment_confidence_when_the_group_already_matches(self) -> None:
        receipt = _receipt(**_PAYMENT_COLUMNS)
        changes = plan_changes(
            receipt, _paid_meta(payment=_PAYMENT), _INGESTED, overwrite=False
        )
        assert changes["field_confidence.payment"] == (None, 0.7)
        assert not set(_PAYMENT_COLUMNS) & set(changes)


class TestStaleGroupConfidence:
    def test_removed_when_the_group_changes_but_doesnt_match(self) -> None:
        receipt = _receipt(
            payment_method="bank",
            payment_last4="9999",
            payer="Jane Doe",
            field_confidence={**_FC, "payment": 0.6},
        )
        history = History(
            ingested=True,
            applied=_INGESTED.applied | {"payment_method", "payment_last4", "payer"},
        )
        # A run reads a new method and last four, but no payer: a null
        # result never clears the stored payer.
        meta = _paid_meta(payment={"method": "card", "last4": "1234"})
        changes = plan_changes(receipt, meta, history, overwrite=True)
        assert changes["payment_method"] == ("bank", "card")
        assert changes["payment_last4"] == ("9999", "1234")
        assert "payer" not in changes
        assert changes["field_confidence.payment"] == (0.6, None)

    def test_kept_when_nothing_in_the_group_changes(self) -> None:
        receipt = _receipt(
            payment_method="bank", field_confidence={**_FC, "payment": 0.6}
        )
        meta = _paid_meta(payment={"method": "card"})
        changes = plan_changes(receipt, meta, _INGESTED, overwrite=False)
        assert "payment_method" not in changes
        assert "field_confidence.payment" not in changes


class TestManualAndCleared:
    def test_a_manual_order_number_is_never_written(self) -> None:
        receipt = _receipt(order_number="HAND-1", manual_fields=["order_number"])
        changes = plan_changes(
            receipt, _paid_meta(order_number="A-100"), _NO_HISTORY, overwrite=True
        )
        assert "order_number" not in changes
        assert "field_confidence.order_number" not in changes

    def test_a_cleared_field_ingest_left_null_is_replaced(self) -> None:
        # Ingest found no order number, so it never applied one. A person set
        # it, then cleared the mark: the cleared field counts as applied.
        receipt = _receipt(order_number="HAND-1")
        cleared = History(ingested=True, applied=_INGESTED.applied | {"order_number"})
        changes = plan_changes(
            receipt, _paid_meta(order_number="A-100"), cleared, overwrite=True
        )
        assert changes["order_number"] == ("HAND-1", "A-100")
        assert changes["field_confidence.order_number"] == (None, 0.9)

    def test_without_the_clear_it_stays(self) -> None:
        receipt = _receipt(order_number="HAND-1")
        changes = plan_changes(
            receipt, _paid_meta(order_number="A-100"), _INGESTED, overwrite=True
        )
        assert "order_number" not in changes


class TestDoneGateConfidence:
    def test_a_null_confidence_for_a_changed_group_removes_the_entry(self) -> None:
        # The stored 0.9 was the confidence in "card" alone. A run fills the
        # last four and the payer but gives no payment confidence: the old
        # entry no longer describes what the receipt holds.
        receipt = _receipt(
            payment_method="card", field_confidence={**_FC, "payment": 0.9}
        )
        meta = _meta(
            payment={"method": "card", "last4": "1234", "payer": "Jane Doe"},
            field_confidence=FieldConfidence(**_FC),
        )
        changes = plan_changes(receipt, meta, _INGESTED, overwrite=False)
        assert changes["payment_last4"] == (None, "1234")
        assert changes["field_confidence.payment"] == (0.9, None)

    def test_nothing_changed_keeps_it(self) -> None:
        receipt = _receipt(
            payment_method="card", field_confidence={**_FC, "payment": 0.9}
        )
        meta = _meta(
            payment={"method": "card"}, field_confidence=FieldConfidence(**_FC)
        )
        changes = plan_changes(receipt, meta, _INGESTED, overwrite=False)
        assert "field_confidence.payment" not in changes
