"""Order number, payment and hand edits against the devtest Postgres.

The receipt data-model design, §4. Receipts are seeded directly, with
renditions in a temporary store, and the LLM is the mock from the
re-extraction tests. Vendors, amounts and names are placeholders.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from receipt_index.edits import clear_manual, edit_receipt
from receipt_index.extraction import EXTRACTION_PROMPT_VERSION
from receipt_index.models import FieldConfidence, Payment
from receipt_index.repository import (
    count_receipts,
    get_receipt_by_id,
    get_receipt_events,
    insert_extraction,
    search_receipts,
)
from receipt_index.store import LocalEvidenceStore

from .test_reextract import _MODEL, Llm, _good_row, _meta, _run, _seed, _select

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg

    from receipt_index.models import Receipt

    Conn = psycopg.Connection[dict[str, Any]]

pytestmark = pytest.mark.integration


def _set(conn: Conn, receipt: Receipt, **columns: Any) -> None:
    sets = ", ".join(f"{name} = %({name})s" for name in columns)
    conn.execute(
        f"UPDATE receipt.receipts SET {sets} WHERE id = %(id)s",
        {**columns, "id": receipt.id},
    )
    conn.commit()


def _paid_meta(subject: str, **overrides: Any) -> Any:
    fc = FieldConfidence(
        vendor=0.95,
        amount=0.9,
        paid_amount=0.85,
        date=0.9,
        order_number=0.9,
        payment=0.8,
    )
    return _meta(subject, field_confidence=fc, **overrides)


class TestReextractFills:
    def test_order_number_and_payment(self, pg_conn: Conn, tmp_path: Path) -> None:
        store = LocalEvidenceStore(tmp_path)
        receipt = _seed(pg_conn, store, "Paid by card")
        llm = Llm(
            {
                "Paid by card": _paid_meta(
                    "Paid by card",
                    order_number="A-100",
                    payment=Payment(method="card", last4="1234", payer="Jane Doe"),
                )
            }
        )
        result = _run(pg_conn, store, llm, _select(pg_conn, ids=[receipt.id]))
        assert [o.status for o in result.outcomes] == ["applied"]
        after = get_receipt_by_id(pg_conn, receipt.id)
        assert after is not None
        assert (
            after.order_number,
            after.payment_method,
            after.payment_last4,
            after.payer,
        ) == ("A-100", "card", "1234", "Jane Doe")
        assert after.field_confidence["payment"] == pytest.approx(0.8)
        assert after.field_confidence["order_number"] == pytest.approx(0.9)


class TestSearchFilters:
    def test_order_number_last4_and_payer(self, pg_conn: Conn, tmp_path: Path) -> None:
        store = LocalEvidenceStore(tmp_path)
        first = _seed(pg_conn, store, "First")
        second = _seed(pg_conn, store, "Second")
        third = _seed(pg_conn, store, "Third")
        _set(
            pg_conn, first, order_number="A-100", payment_last4="1234", payer="Jane Doe"
        )
        _set(
            pg_conn,
            second,
            order_number="A-100",
            payment_last4="9876",
            payer="John 100% Doe",
        )
        _set(pg_conn, third, order_number="A-1000", payment_last4="1234")

        def ids(**kw: Any) -> set[Any]:
            found = {r.id for r in search_receipts(pg_conn, **kw)}
            assert count_receipts(pg_conn, **kw) == len(found)
            return found

        assert ids(order_number="A-100") == {first.id, second.id}
        assert ids(last4="1234") == {first.id, third.id}
        assert ids(payer="jane") == {first.id}
        assert ids(payer="doe") == {first.id, second.id}
        # % and _ are matched literally, not as wildcards.
        assert ids(payer="100%") == {second.id}
        assert ids(payer="J_hn") == set()
        assert ids(order_number="A-100", last4="1234") == {first.id}
        pg_conn.commit()


class TestEditAndReextract:
    def test_edit_survives_overwrite_and_clearing_hands_it_back(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        # Ingest found no paid amount, so it never applied one.
        receipt = _seed(
            pg_conn,
            store,
            "Hand edited",
            field_confidence={
                "vendor": 0.9,
                "amount": 0.9,
                "paid_amount": 0.2,
                "date": 0.9,
            },
        )
        _good_row(pg_conn, receipt.id, version=EXTRACTION_PROMPT_VERSION, model=_MODEL)
        llm = Llm({"Hand edited": _paid_meta("Hand edited")})  # paid 30.00

        edited = edit_receipt(
            pg_conn,
            receipt.id,
            {"paid_amount": Decimal("25.00")},
            actor="operator",
            reason="statement shows 25.00",
        )
        assert edited.receipt.manual_fields == ["paid_amount"]
        assert "paid_amount" not in edited.receipt.field_confidence

        # Current after ingest: only --force selects it again.
        assert _select(pg_conn, ids=[receipt.id]) == []
        result = _run(
            pg_conn,
            store,
            llm,
            _select(pg_conn, ids=[receipt.id], force=True),
            overwrite=True,
            force=True,
        )
        assert [o.status for o in result.outcomes] in (["applied"], ["unchanged"])
        after = get_receipt_by_id(pg_conn, receipt.id)
        assert after is not None
        assert after.paid_amount == Decimal("25.00")

        clear_manual(
            pg_conn, receipt.id, ["paid_amount"], actor="operator", reason=None
        )
        result = _run(
            pg_conn,
            store,
            llm,
            _select(pg_conn, ids=[receipt.id], force=True),
            overwrite=True,
            force=True,
        )
        assert [o.status for o in result.outcomes] == ["applied"]
        after = get_receipt_by_id(pg_conn, receipt.id)
        assert after is not None
        assert after.paid_amount == Decimal("30.00")
        assert after.manual_fields == []
        assert after.field_confidence["paid_amount"] == pytest.approx(0.85)

        events = get_receipt_events(pg_conn, receipt.id)
        pg_conn.commit()
        assert [(e.event_type, e.payload["field"]) for e in events] == [
            ("field_edited", "paid_amount"),
            ("manual_cleared", "paid_amount"),
        ]
        assert events[0].payload == {
            "field": "paid_amount",
            "old": None,
            "new": "25.00",
            "old_confidence": 0.2,
        }
        assert events[0].reason == "statement shows 25.00"
        assert events[0].actor_kind == "person"

    def test_confidence_below_skips_a_hand_edited_field(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        edited = _seed(pg_conn, store, "Edited")
        untouched = _seed(pg_conn, store, "Untouched")
        edit_receipt(
            pg_conn,
            edited.id,
            {"paid_amount": Decimal("5.00")},
            actor="operator",
            reason=None,
        )
        edit_receipt(
            pg_conn,
            untouched.id,
            {"vendor": "Acme Parking"},
            actor="operator",
            reason=None,
        )
        ids = [edited.id, untouched.id]

        below = {
            r.id
            for r in _select(pg_conn, ids=ids, confidence_below=("paid_amount", 0.5))
        }
        assert below == {untouched.id}
        # date maps to receipt_date in manual_fields.
        edit_receipt(
            pg_conn,
            untouched.id,
            {"receipt_date": edited.receipt_date},
            actor="operator",
            reason=None,
        )
        below = {
            r.id for r in _select(pg_conn, ids=ids, confidence_below=("date", 0.5))
        }
        assert below == {edited.id}


class TestNeedsReview:
    def test_a_hand_edited_paid_amount_leaves_the_list(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        receipt = _seed(pg_conn, store, "Gated", paid="12.00")
        # Its latest extraction fell below the confidence gate.
        insert_extraction(
            pg_conn,
            receipt_id=receipt.id,
            origin="reextract",
            input_kind="rendition",
            prompt_version=EXTRACTION_PROMPT_VERSION,
            llm_model=_MODEL,
            output={"confidence": 0.2},
            applied_fields=[],
            replaced={},
            llm_input_tokens=1000,
            llm_output_tokens=100,
            llm_cache_read_tokens=0,
            llm_requests=1,
        )
        pg_conn.commit()

        def listed() -> bool:
            found = {r.id for r in search_receipts(pg_conn, needs_review=True)}
            assert count_receipts(pg_conn, needs_review=True) == len(found)
            pg_conn.commit()
            return receipt.id in found

        assert listed()
        edit_receipt(
            pg_conn,
            receipt.id,
            {"paid_amount": Decimal("12.50")},
            actor="operator",
            reason=None,
        )
        assert not listed()
        clear_manual(
            pg_conn, receipt.id, ["paid_amount"], actor="operator", reason=None
        )
        assert listed()
