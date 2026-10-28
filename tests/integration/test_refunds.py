"""Refunds, credits and statements against the devtest Postgres.

The receipt data-model design, §6, widened by the operator's decision on the
refunds request. The LLM is a mock, so these tests prove that kind, the link
and the totals are stored and computed, not that a model classifies a
document correctly. Vendors, order numbers and amounts are placeholders.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.edits import EditError, edit_receipt
from receipt_index.models import FieldConfidence, ReceiptMetadata
from receipt_index.pipeline import run_ingest
from receipt_index.refunds import RefundError, link, link_by_rule, unlink
from receipt_index.repository import (
    get_receipt_by_id,
    get_receipt_events,
    insert_receipt,
    list_refunds,
    lock_matching_purchase,
    search_receipts,
    set_refund_of,
)
from receipt_index.store import LocalEvidenceStore

from .conftest import seed_email
from .test_paid_amount import _imap_config
from .test_reextract import Llm, _meta, _run, _seed, _select

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg

    from receipt_index.models import Receipt

    Conn = psycopg.Connection[dict[str, Any]]

pytestmark = pytest.mark.integration

_VENDOR = "Acme Parking (U.S.), LLC"


def _insert(
    conn: Conn,
    *,
    kind: str = "purchase",
    order_number: str | None = "12345",
    vendor: str = _VENDOR,
    paid: str | None = "25.00",
    currency: str = "USD",
) -> Receipt:
    receipt = insert_receipt(
        conn,
        source_id=f"<{uuid.uuid4().hex}@example.com>",
        source_type="imap",
        source_name="personal-email",
        vendor=vendor,
        amount=Decimal("25.00"),
        paid_amount=None if paid is None else Decimal(paid),
        currency=currency,
        receipt_date=date(2026, 1, 15),
        description=None,
        confidence=0.9,
        pdf_path="2026/01/x.pdf",
        email_subject=None,
        email_sender=None,
        email_date=None,
        order_number=order_number,
        kind=kind,
        commit=False,
    )
    link_by_rule(conn, receipt)
    conn.commit()
    return receipt


def _refund_of(conn: Conn, receipt: Receipt) -> Any:
    after = get_receipt_by_id(conn, receipt.id)
    assert after is not None
    return after.refund_of


def _events(conn: Conn, receipt: Receipt) -> list[tuple[str, str]]:
    return [(e.event_type, e.actor_kind) for e in get_receipt_events(conn, receipt.id)]


# ---------------------------------------------------------------------------
# The request's acceptance case, through ingest
# ---------------------------------------------------------------------------

_PURCHASE_SUBJECT = "Your order 12345"
_REFUND_SUBJECT = "Refund issued, Order 12345"


def _fc() -> FieldConfidence:
    return FieldConfidence(
        vendor=0.95, amount=0.95, paid_amount=0.9, date=0.95, order_number=0.9, kind=0.9
    )


_EXTRACTIONS = {
    _PURCHASE_SUBJECT: ReceiptMetadata(
        vendor=_VENDOR,
        amount=Decimal("25.00"),
        paid_amount=Decimal("25.00"),
        date=date(2026, 1, 10),
        order_number="12345",
        confidence=0.95,
        field_confidence=_fc(),
    ),
    _REFUND_SUBJECT: ReceiptMetadata(
        vendor=_VENDOR,
        amount=Decimal("25.00"),
        paid_amount=Decimal("25.00"),
        date=date(2026, 1, 20),
        order_number="12345",
        kind="refund",
        confidence=0.95,
        field_confidence=_fc(),
    ),
}


def _agent() -> MagicMock:
    def run_sync(prompt: str) -> MagicMock:
        (meta,) = [m for s, m in _EXTRACTIONS.items() if f"Subject: {s}" in prompt]
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 60
        usage.cache_read_tokens = 0
        usage.requests = 1
        result = MagicMock()
        result.output = meta
        result.usage = usage
        return result

    agent = MagicMock()
    agent.run_sync.side_effect = run_sync
    return agent


class TestAcceptance:
    def test_a_purchase_and_its_refund_net_to_zero(
        self, greenmail_available: bool, pg_conn: Conn, tmp_path: Path
    ) -> None:
        user = f"refund-{uuid.uuid4().hex[:8]}@localhost"
        for subject in (_PURCHASE_SUBJECT, _REFUND_SUBJECT):
            seed_email(
                to_addr=user,
                from_addr="orders@example.com",
                subject=subject,
                text_body=f"{subject}\n$25.00\n",
            )
        result = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_imap_config(user)),
            store=LocalEvidenceStore(tmp_path),
            source_name="greenmail-test",
            source_type="imap",
            agent=_agent(),
            llm_model="test-model",
        )
        assert (result.processed, result.failed) == (2, 0)

        everything = search_receipts(pg_conn)
        (refund,) = [r for r in everything if r.kind == "refund"]
        (purchase,) = [r for r in everything if r.kind == "purchase"]
        assert refund.signed_amount == Decimal("-25.00")
        assert sum(r.signed_amount for r in everything) == 0
        total = pg_conn.execute(
            "SELECT sum(signed_amount) AS s FROM receipt.receipts"
        ).fetchone()
        assert total == {"s": Decimal("0.00")}

        # Linked at ingest, by the rule, whichever arrived first.
        assert refund.refund_of == purchase.id
        assert _events(pg_conn, refund) == [("refund_linked", "rule")]
        assert purchase.net_paid == Decimal("0.00")
        assert purchase.refunded == Decimal("25.00")
        shown = get_receipt_by_id(pg_conn, purchase.id)
        assert shown is not None
        assert shown.net_paid == Decimal("0.00")

        # Search can leave refunds out.
        purchases = search_receipts(pg_conn, kinds=["purchase"])
        assert [r.id for r in purchases] == [purchase.id]


# ---------------------------------------------------------------------------
# The linking rule
# ---------------------------------------------------------------------------


class TestLinkingRule:
    def test_one_match_links_with_a_rule_event(self, pg_conn: Conn) -> None:
        purchase = _insert(pg_conn)
        refund = _insert(pg_conn, kind="refund")
        assert _refund_of(pg_conn, refund) == purchase.id
        (event,) = get_receipt_events(pg_conn, refund.id)
        assert event.event_type == "refund_linked"
        assert (event.actor, event.actor_kind) == ("rule", "rule")
        assert event.payload == {
            "purchase_id": str(purchase.id),
            "order_number": "12345",
        }

    def test_a_purchase_arriving_later_links_the_waiting_refund(
        self, pg_conn: Conn
    ) -> None:
        refund = _insert(pg_conn, kind="refund")
        assert _refund_of(pg_conn, refund) is None
        purchase = _insert(pg_conn)
        assert _refund_of(pg_conn, refund) == purchase.id

    def test_no_match_stays_unlinked(self, pg_conn: Conn) -> None:
        _insert(pg_conn, order_number="99999")
        refund = _insert(pg_conn, kind="refund")
        assert _refund_of(pg_conn, refund) is None
        assert [r.id for r in list_refunds(pg_conn, unlinked=True)] == [refund.id]

    def test_several_matches_stay_unlinked(self, pg_conn: Conn) -> None:
        # An order charged on each shipment.
        _insert(pg_conn)
        _insert(pg_conn)
        refund = _insert(pg_conn, kind="refund")
        assert _refund_of(pg_conn, refund) is None
        assert _events(pg_conn, refund) == []

    @pytest.mark.parametrize(
        "other",
        [
            {"vendor": "Example Services"},
            {"currency": "CAD"},
            {"paid": "0.00"},  # a confirmation that charged nothing
            {"paid": None},
            {"kind": "credit"},
        ],
    )
    def test_what_does_not_match(self, pg_conn: Conn, other: dict[str, Any]) -> None:
        _insert(pg_conn, **other)
        refund = _insert(pg_conn, kind="refund")
        assert _refund_of(pg_conn, refund) is None

    def test_vendor_matches_in_any_case(self, pg_conn: Conn) -> None:
        purchase = _insert(pg_conn, vendor=_VENDOR.upper())
        refund = _insert(pg_conn, kind="refund")
        assert _refund_of(pg_conn, refund) == purchase.id

    def test_credits_and_statements_never_link(self, pg_conn: Conn) -> None:
        _insert(pg_conn)
        for kind in ("credit", "statement"):
            receipt = _insert(pg_conn, kind=kind)
            assert _refund_of(pg_conn, receipt) is None

    def test_a_purchase_whose_paid_amount_is_filled_links_its_refund(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        # The Done gate's case: the refund waits on a purchase whose paid
        # amount isn't known; re-extraction fills it, and the rule runs.
        store = LocalEvidenceStore(tmp_path)
        purchase = _seed(pg_conn, store, "Your order")
        pg_conn.execute(
            "UPDATE receipt.receipts SET order_number = '12345' WHERE id = %s",
            (purchase.id,),
        )
        pg_conn.commit()
        refund = _insert(pg_conn, kind="refund")
        assert _refund_of(pg_conn, refund) is None
        llm = Llm({"Your order": _meta("Your order", order_number="12345")})
        _run(pg_conn, store, llm, _select(pg_conn, ids=[purchase.id]))
        assert _refund_of(pg_conn, refund) == purchase.id

    def test_the_lock_rechecks_the_whole_match(self, pg_conn: Conn) -> None:
        # Between the rule's read and its lock, a match can stop matching.
        purchase = _insert(pg_conn)
        refund = _insert(pg_conn, kind="refund", order_number="other")
        for change in (
            "vendor = 'Example Services'",
            "currency = 'CAD'",
            "paid_amount = 0",
            "order_number = 'changed'",
            "kind = 'credit'",
        ):
            pg_conn.execute(
                f"UPDATE receipt.receipts SET {change} WHERE id = %s",
                (purchase.id,),
            )
            stale = refund.model_copy(update={"order_number": "12345"})
            assert not lock_matching_purchase(pg_conn, stale, purchase.id), change
            pg_conn.rollback()
        stale = refund.model_copy(update={"order_number": "12345"})
        assert lock_matching_purchase(pg_conn, stale, purchase.id)
        pg_conn.rollback()

    def test_the_rules_write_never_overwrites_a_link(self, pg_conn: Conn) -> None:
        first = _insert(pg_conn, order_number="A")
        second = _insert(pg_conn, order_number="B")
        refund = _insert(pg_conn, kind="refund", order_number="C")
        link(pg_conn, refund.id, first.id, actor="operator", reason=None)
        assert not set_refund_of(pg_conn, refund.id, second.id, only_if_unlinked=True)
        pg_conn.commit()
        assert _refund_of(pg_conn, refund) == first.id

    def test_a_refund_a_person_unlinked_stays_unlinked(self, pg_conn: Conn) -> None:
        # The operator's ruling: after a person unlinks a refund, only
        # `refund link` links it again; the rule never does.
        first = _insert(pg_conn, paid="25.00")
        refund = _insert(pg_conn, kind="refund")
        assert _refund_of(pg_conn, refund) == first.id
        unlink(pg_conn, refund.id, actor="operator", reason="wrong order")
        # Another purchase pass for the same order: the first purchase is
        # still the only match, but the rule leaves the refund alone.
        again = get_receipt_by_id(pg_conn, first.id)
        assert again is not None
        assert link_by_rule(pg_conn, again) == []
        stored = get_receipt_by_id(pg_conn, refund.id)
        assert stored is not None
        assert link_by_rule(pg_conn, stored) == []
        pg_conn.commit()
        assert _refund_of(pg_conn, refund) is None
        # A person can still link it.
        link(pg_conn, refund.id, first.id, actor="operator", reason=None)
        assert _refund_of(pg_conn, refund) == first.id


# ---------------------------------------------------------------------------
# Linking by hand
# ---------------------------------------------------------------------------


class TestByHand:
    def test_link_and_unlink_write_person_events(self, pg_conn: Conn) -> None:
        purchase = _insert(pg_conn, order_number="A")
        refund = _insert(pg_conn, kind="refund", order_number="B")
        link(pg_conn, refund.id, purchase.id, actor="operator", reason="same order")
        assert _refund_of(pg_conn, refund) == purchase.id
        unlink(pg_conn, refund.id, actor="operator", reason=None)
        assert _refund_of(pg_conn, refund) is None
        assert _events(pg_conn, refund) == [
            ("refund_linked", "person"),
            ("refund_unlinked", "person"),
        ]

    @pytest.mark.parametrize(
        ("refund_kw", "purchase_kw", "message"),
        [
            ({"kind": "purchase"}, {}, "not a refund"),
            ({"kind": "refund"}, {"kind": "credit"}, "not a purchase"),
            ({"kind": "refund"}, {"currency": "CAD"}, "currencies differ"),
        ],
    )
    def test_refusals(
        self,
        pg_conn: Conn,
        refund_kw: dict[str, Any],
        purchase_kw: dict[str, Any],
        message: str,
    ) -> None:
        purchase = _insert(pg_conn, order_number="P", **purchase_kw)
        refund = _insert(pg_conn, order_number="R", **refund_kw)
        with pytest.raises(RefundError, match=message):
            link(pg_conn, refund.id, purchase.id, actor="operator", reason=None)
        assert _events(pg_conn, refund) == []

    def test_an_already_linked_refund_is_refused(self, pg_conn: Conn) -> None:
        purchase = _insert(pg_conn)
        refund = _insert(pg_conn, kind="refund")
        other = _insert(pg_conn, order_number="other")
        with pytest.raises(RefundError, match="unlink it first"):
            link(pg_conn, refund.id, other.id, actor="operator", reason=None)
        assert _refund_of(pg_conn, refund) == purchase.id

    def test_unlink_of_an_unlinked_refund_is_refused(self, pg_conn: Conn) -> None:
        refund = _insert(pg_conn, kind="refund", order_number=None)
        with pytest.raises(RefundError, match="not linked"):
            unlink(pg_conn, refund.id, actor="operator", reason=None)


class TestEditKind:
    def test_edit_kind_links_by_the_rule(self, pg_conn: Conn) -> None:
        purchase = _insert(pg_conn)
        receipt = _insert(pg_conn)  # misread as a second purchase
        result = edit_receipt(
            pg_conn, receipt.id, {"kind": "refund"}, actor="operator", reason=None
        )
        assert result.receipt.kind == "refund"
        assert "kind" in result.receipt.manual_fields
        # Both receipts match each other's order: with the second now a
        # refund, the first is the only purchase left.
        assert _refund_of(pg_conn, receipt) == purchase.id
        # The edit returns the receipt as linked, with the rule's event.
        assert result.receipt.refund_of == purchase.id
        assert [(e.event_type, e.actor_kind) for e in result.events] == [
            ("field_edited", "person"),
            ("refund_linked", "rule"),
        ]

    def test_a_linked_receipt_keeps_its_kind(self, pg_conn: Conn) -> None:
        purchase = _insert(pg_conn)
        refund = _insert(pg_conn, kind="refund")
        for receipt in (refund, purchase):
            with pytest.raises(EditError, match="unlink"):
                edit_receipt(
                    pg_conn, receipt.id, {"kind": "credit"}, actor="op", reason=None
                )

    def test_an_unknown_kind_is_refused(self, pg_conn: Conn) -> None:
        receipt = _insert(pg_conn)
        with pytest.raises(ValueError, match="kind must be one of"):
            edit_receipt(
                pg_conn, receipt.id, {"kind": "other"}, actor="op", reason=None
            )


# ---------------------------------------------------------------------------
# Re-extraction (the design's Testing Strategy)
# ---------------------------------------------------------------------------


class TestReextract:
    def test_fill_only_sets_refund_kind_and_links(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        purchase = _insert(pg_conn, vendor=_VENDOR)
        # A receipt with no extraction history, stored as a purchase.
        old = _seed(pg_conn, store, "Refund issued")
        llm = Llm(
            {
                "Refund issued": _meta(
                    "Refund issued",
                    kind="refund",
                    order_number="12345",
                    paid_amount=Decimal("25.00"),
                )
            }
        )
        result = _run(pg_conn, store, llm, _select(pg_conn, refund_candidates=True))
        assert [o.receipt_id for o in result.outcomes] == [old.id]
        assert result.outcomes[0].changes["kind"] == ("purchase", "refund")
        assert _refund_of(pg_conn, old) == purchase.id
        assert _events(pg_conn, old) == [("refund_linked", "rule")]

    def test_refund_candidates_select_by_words(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        hits = [
            _seed(pg_conn, store, subject)
            for subject in (
                "Your REFUND",
                "Refunded: order 12345",
                "Statement for June",
                "Credit memo",
                "Order cancelled",
                "Cancellation confirmed",
                "Item returned",
                "Your eStatement is ready",
                "Statements for 2025",
                "Credits applied to your account",
                "Chargebacks and reversals",
                "Cancellations this month",
            )
        ]
        # Whole words only, and a credit card is not a credit.
        for subject in (
            "Parking receipt",
            "Paid by credit card",
            "See our returns policy",
            "Precancelled stamps",
        ):
            _seed(pg_conn, store, subject)
        already = _seed(pg_conn, store, "Refund two")
        pg_conn.execute(
            "UPDATE receipt.receipts SET kind = 'refund' WHERE id = %s", (already.id,)
        )
        pg_conn.commit()
        selected = {r.id for r in _select(pg_conn, refund_candidates=True)}
        assert selected == {r.id for r in hits}
