"""Linking a refund to the purchase it returns money for (data-model design, §6).

A refund is its own receipt with ``kind = 'refund'``; ``refund_of`` points at
its purchase, and the ``v_receipt_net`` view takes linked refunds off the
purchase's paid amount.

``link_by_rule`` is the automatic rule. It runs inside the caller's
transaction (ingest, re-extraction, a hand edit) and doesn't commit: an
unlinked refund is linked when exactly one purchase has its order number,
vendor (any case) and currency, and a paid amount above zero. No match, or
several, leave it unlinked for a person, and the rule never links a refund a
person has unlinked. Each link is a ``refund_linked``
event with ``actor_kind = 'rule'``.

``link`` and ``unlink`` are the ``refund`` commands: a person's change, one
event each, committed together with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from receipt_index.models import scrub_card_numbers
from receipt_index.repository import (
    find_refund_purchases,
    find_unlinked_refunds,
    insert_event,
    lock_matching_purchase,
    lock_receipt,
    set_refund_of,
    was_unlinked,
)

if TYPE_CHECKING:
    from uuid import UUID

    import psycopg

    from receipt_index.models import ActorKind, Receipt, ReceiptEvent

# The actor recorded for a link the rule makes.
RULE_ACTOR = "rule"

# The fields the rule matches on. A change to any of them, by re-extraction
# or a hand edit, runs the rule again (design \u00a76): a purchase that gains a
# paid amount may now be a waiting refund's only match.
RULE_FIELDS = frozenset({"kind", "order_number", "vendor", "currency", "paid_amount"})


class RefundError(Exception):
    """A link or unlink that can't be made. Nothing was written."""


def _link_event(
    conn: psycopg.Connection[dict[str, Any]],
    refund: Receipt,
    purchase_id: UUID,
    *,
    actor: str,
    actor_kind: ActorKind,
    reason: str | None,
) -> ReceiptEvent:
    set_refund_of(conn, refund.id, purchase_id)
    return insert_event(
        conn,
        receipt_id=refund.id,
        event_type="refund_linked",
        payload={
            "purchase_id": str(purchase_id),
            "order_number": refund.order_number,
        },
        actor=actor,
        actor_kind=actor_kind,
        reason=reason,
    )


def _rule_link(
    conn: psycopg.Connection[dict[str, Any]], refund: Receipt, purchase_id: UUID
) -> ReceiptEvent | None:
    """The rule's link: written only while the refund is still unlinked (a
    person may have linked it meanwhile), with its event only if written."""
    if not set_refund_of(conn, refund.id, purchase_id, only_if_unlinked=True):
        return None
    return insert_event(
        conn,
        receipt_id=refund.id,
        event_type="refund_linked",
        payload={
            "purchase_id": str(purchase_id),
            "order_number": refund.order_number,
        },
        actor=RULE_ACTOR,
        actor_kind="rule",
        reason=None,
    )


def link_by_rule(
    conn: psycopg.Connection[dict[str, Any]], receipt: Receipt
) -> list[ReceiptEvent]:
    """Run the linking rule for a receipt just written; the caller commits.

    For an unlinked refund, look for its purchase. For a purchase, link each
    unlinked refund for which it is now the only matching purchase. Anything
    else (a linked refund, a credit, a statement, a merged receipt) links
    nothing.
    """
    if receipt.merged_into_id is not None:
        return []
    if receipt.kind == "refund":
        # A refund a person unlinked is theirs: only `refund link` links it.
        if receipt.refund_of is not None or was_unlinked(conn, receipt.id):
            return []
        matches = find_refund_purchases(conn, receipt)
        if len(matches) != 1 or not lock_matching_purchase(conn, receipt, matches[0]):
            return []
        event = _rule_link(conn, receipt, matches[0])
        return [] if event is None else [event]
    if receipt.kind != "purchase":
        return []
    events: list[ReceiptEvent] = []
    for refund in find_unlinked_refunds(conn, receipt):
        if find_refund_purchases(conn, refund) == [receipt.id]:
            event = _rule_link(conn, refund, receipt.id)
            if event is not None:
                events.append(event)
    return events


def _locked(conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID) -> Receipt:
    receipt = lock_receipt(conn, receipt_id)
    if receipt is None:
        conn.rollback()
        msg = f"Receipt not found: {receipt_id}"
        raise RefundError(msg)
    return receipt


def _refuse(conn: psycopg.Connection[dict[str, Any]], msg: str) -> RefundError:
    conn.rollback()
    return RefundError(msg)


def link(
    conn: psycopg.Connection[dict[str, Any]],
    refund_id: UUID,
    purchase_id: UUID,
    *,
    actor: str,
    reason: str | None,
) -> ReceiptEvent:
    """Link a refund to a purchase by hand, in one transaction.

    Refuses, writing nothing, unless the first is an unlinked refund, the
    second a purchase, and their currencies match.
    """
    if refund_id == purchase_id:
        msg = "a refund can't be its own purchase"
        raise RefundError(msg)
    refund = _locked(conn, refund_id)
    purchase = _locked(conn, purchase_id)
    if refund.kind != "refund":
        raise _refuse(conn, f"{refund_id} is a {refund.kind}, not a refund")
    if purchase.kind != "purchase":
        raise _refuse(conn, f"{purchase_id} is a {purchase.kind}, not a purchase")
    if refund.refund_of is not None:
        raise _refuse(
            conn,
            f"{refund_id} is already linked to {refund.refund_of}: unlink it first",
        )
    if refund.currency != purchase.currency:
        raise _refuse(
            conn,
            f"currencies differ: the refund is in {refund.currency}, "
            f"the purchase in {purchase.currency}",
        )
    event = _link_event(
        conn,
        refund,
        purchase_id,
        actor=scrub_card_numbers(actor),
        actor_kind="person",
        reason=None if reason is None else scrub_card_numbers(reason),
    )
    conn.commit()
    return event


def unlink(
    conn: psycopg.Connection[dict[str, Any]],
    refund_id: UUID,
    *,
    actor: str,
    reason: str | None,
) -> ReceiptEvent:
    """Unlink a refund from its purchase by hand, in one transaction."""
    refund = _locked(conn, refund_id)
    if refund.refund_of is None:
        raise _refuse(conn, f"{refund_id} is not linked to a purchase")
    set_refund_of(conn, refund.id, None)
    event = insert_event(
        conn,
        receipt_id=refund.id,
        event_type="refund_unlinked",
        payload={"purchase_id": str(refund.refund_of)},
        actor=scrub_card_numbers(actor),
        actor_kind="person",
        reason=None if reason is None else scrub_card_numbers(reason),
    )
    conn.commit()
    return event
