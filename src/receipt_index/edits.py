"""Hand edits to a receipt (receipt data-model design, §4).

A person corrects an extracted field with ``receipt-index edit``. Each edited
field is written, added to ``manual_fields`` so that re-extraction leaves it
alone, and recorded as one ``field_edited`` event with its old and new value,
all in one transaction. The field's ``field_confidence`` entry is removed: it
was the extractor's confidence in a value the receipt no longer holds.
``clear_manual`` hands a field back to re-extraction with a ``manual_cleared``
event.

The receipt is read ``FOR UPDATE``, as re-extraction reads it before it
applies a result, so an edit and a run never interleave on one receipt.
Each function commits on success. On an error it leaves the transaction
open: the caller rolls it back (the CLI's connection context does), so no
event is left without its change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from receipt_index.models import scrub_card_numbers
from receipt_index.repository import insert_event, lock_receipt, update_edited_fields

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from uuid import UUID

    import psycopg

    from receipt_index.models import Receipt, ReceiptEvent

# The fields a person can edit, by the names manual_fields uses, and the
# field_confidence entry each one has.
EDITABLE_FIELDS: dict[str, str] = {
    "paid_amount": "paid_amount",
    "vendor": "vendor",
    "receipt_date": "date",
    "order_number": "order_number",
}

# Other names a person may use for a field.
_ALIASES = {"date": "receipt_date"}


class EditError(Exception):
    """An edit that can't be made: the receipt is gone, or a field isn't
    hand-edited. Nothing was written."""


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: Any
    new: Any

    @property
    def unchanged(self) -> bool:
        return bool(self.old == self.new)


@dataclass(frozen=True)
class EditResult:
    receipt: Receipt
    changes: list[FieldChange]
    events: list[ReceiptEvent]


def field_name(name: str) -> str:
    """The manual_fields name for ``name`` (``date`` is ``receipt_date``)."""
    return _ALIASES.get(name, name)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _scrubbed(text: str | None) -> str | None:
    return None if text is None else scrub_card_numbers(text)


def _history_value(receipt: Receipt, name: str) -> Any:
    """A field's current value for an event; a vendor without card numbers
    (a receipt extracted before vendor was scrubbed may hold one)."""
    value = getattr(receipt, name, None)
    return _scrubbed(value) if name == "vendor" else value


def _locked(conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID) -> Receipt:
    receipt = lock_receipt(conn, receipt_id)
    if receipt is None:
        conn.rollback()
        msg = f"Receipt not found: {receipt_id}"
        raise EditError(msg)
    return receipt


def edit_receipt(
    conn: psycopg.Connection[dict[str, Any]],
    receipt_id: UUID,
    values: Mapping[str, Any],
    *,
    actor: str,
    reason: str | None,
) -> EditResult:
    """Set ``values`` on the receipt by hand, in one transaction.

    A value equal to the current one still marks the field manual: the
    person has confirmed it, and re-extraction should leave it alone.
    """
    if not values:
        msg = "nothing to edit"
        raise ValueError(msg)
    unknown = sorted(set(values) - set(EDITABLE_FIELDS))
    if unknown:
        msg = f"not an editable field: {', '.join(unknown)}"
        raise ValueError(msg)

    # Hand-entered text lands in the append-only history too: no card numbers
    # (order_number is left as given, as it is at extraction).
    values = {
        name: scrub_card_numbers(new) if name == "vendor" else new
        for name, new in values.items()
    }
    reason = _scrubbed(reason)
    actor = scrub_card_numbers(actor)
    receipt = _locked(conn, receipt_id)
    manual = list(receipt.manual_fields)
    confidence = dict(receipt.field_confidence)
    changes: list[FieldChange] = []
    events: list[ReceiptEvent] = []
    for name, new in values.items():
        old = getattr(receipt, name)
        old_confidence = confidence.pop(EDITABLE_FIELDS[name], None)
        if name not in manual:
            manual.append(name)
        changes.append(FieldChange(name, old, new))
        events.append(
            insert_event(
                conn,
                receipt_id=receipt_id,
                event_type="field_edited",
                payload={
                    "field": name,
                    "old": _jsonable(_scrubbed(old) if name == "vendor" else old),
                    "new": _jsonable(new),
                    "old_confidence": old_confidence,
                },
                actor=actor,
                actor_kind="person",
                reason=reason,
            )
        )
    updated = update_edited_fields(
        conn,
        receipt_id,
        dict(values),
        manual_fields=manual,
        field_confidence=confidence,
    )
    conn.commit()
    return EditResult(updated, changes, events)


def clear_manual(
    conn: psycopg.Connection[dict[str, Any]],
    receipt_id: UUID,
    fields: Iterable[str],
    *,
    actor: str,
    reason: str | None,
) -> EditResult:
    """Hand each field back to re-extraction, in one transaction.

    Refuses, writing nothing, when any field isn't hand-edited.
    """
    reason = _scrubbed(reason)
    actor = scrub_card_numbers(actor)
    names = list(dict.fromkeys(field_name(f) for f in fields))
    if not names:
        msg = "no field to clear"
        raise ValueError(msg)
    receipt = _locked(conn, receipt_id)
    manual = list(receipt.manual_fields)
    for name in names:
        if name not in manual:
            conn.rollback()
            msg = f"{name} is not hand-edited"
            raise EditError(msg)
    events = [
        insert_event(
            conn,
            receipt_id=receipt_id,
            event_type="manual_cleared",
            payload={"field": name, "value": _jsonable(_history_value(receipt, name))},
            actor=actor,
            actor_kind="person",
            reason=reason,
        )
        for name in names
    ]
    updated = update_edited_fields(
        conn,
        receipt_id,
        {},
        manual_fields=[f for f in manual if f not in names],
        field_confidence=dict(receipt.field_confidence),
    )
    conn.commit()
    return EditResult(updated, [], events)
