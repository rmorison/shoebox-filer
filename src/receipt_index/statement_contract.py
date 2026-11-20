"""The statement read contract's shapes, and the read check on stored text.

The MCP statement tools (``mcp_server``) and the ``receipt-index statements``
commands (``cli``) both serialise a revision through these functions, so the
two outputs can't drift apart. The contract is
``docs/engineering/designs/statement-read-contract.md``.

The read check: before a revision's data leaves, its free text is held to
the mask's own bound (``within_mask_bound``) and its stored totals to the
contract's money format. A field that fails withholds the whole call; nothing
is re-masked or repaired, since the digest covers the stored values.

This module imports nothing from MCP, so the CLI doesn't load the SDK.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from receipt_index.statement_extraction import within_mask_bound

if TYPE_CHECKING:
    from collections.abc import Callable

    from receipt_index.statement_repository import StatementChange

STATEMENT_CONTRACT_VERSION = 1
"""The statement read contract's version, in every statement response."""

_CENT = Decimal("0.01")


class _Unreadable:
    """A stored value the contract can't carry: its call is withheld."""

    def __repr__(self) -> str:
        return "<unreadable>"


_UNREADABLE: Final = _Unreadable()
# What the account's NUMERIC(14,2) money columns hold: a total in the JSONB
# is held to the same shape. Its stored spelling must be ASCII digits, an
# optional minus and at most two places, checked before Decimal reads it:
# Decimal also takes exponents, underscores, whitespace, other scripts'
# digits, NaN and infinity.
_STORED_MONEY: Final = re.compile(r"-?[0-9]{1,12}(?:\.[0-9]{1,2})?")
_MAX_STORED_MONEY: Final = Decimal(10) ** 12


def _stored_money(value: object, *, nullable: bool = True) -> str | _Unreadable | None:
    """A total from the JSONB as contract money, or ``_UNREADABLE``.

    The pipeline stores each as a decimal string. Anything else (another
    JSON type, a missing key, null where ``nullable`` is false, or text that
    isn't a plain decimal of at most two places that a NUMERIC(14,2) holds)
    can't be served as money."""
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _STORED_MONEY.fullmatch(value) is None:
        return _UNREADABLE
    try:
        amount = Decimal(value)
        # Both hold by the pattern; checked again so a change to it fails
        # closed rather than raising past the withheld path.
        if not amount.is_finite() or abs(amount) >= _MAX_STORED_MONEY:
            return _UNREADABLE
        return money(amount)
    except InvalidOperation:
        return _UNREADABLE


def money(value: Decimal | None) -> str | None:
    """A contract money string: two places, never ``-0.00``."""
    if value is None:
        return None
    cents = value.quantize(_CENT)
    if cents.is_zero():
        return "0.00"
    return f"{cents:f}"


def day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def timestamp(value: datetime) -> str:
    """RFC 3339 in UTC, microseconds, with a ``Z``."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_LOAN_FIGURES = (
    "principal_before",
    "principal_after",
    "principal_paid",
    "interest",
    "escrow",
    "fees",
    "advances",
)


def _loan(row: dict[str, Any]) -> dict[str, str | None] | None:
    """A loan account's figures, as the gates digested them.

    The gates digest ``loan`` as null only for a loan account whose
    extraction was capped (it read no figures), and as an object, its
    figures null when not printed, for every other loan account. Any
    other account type has no loan.
    """
    if row["account_type"] != "loan" or row["failure_class"] == "too_many_lines":
        return None
    return {name: money(row[name]) for name in _LOAN_FIGURES}


def _stored_field(total: dict[str, Any], key: str) -> object:
    """A key of a stored total; a missing one is ``_UNREADABLE``, not null."""
    return total.get(key, _UNREADABLE)


def _stored_totals(
    value: object, read: Callable[[dict[str, Any]], dict[str, Any]]
) -> list[dict[str, Any] | _Unreadable] | _Unreadable:
    """A JSONB totals list, each object read by ``read``.

    The CHECK holds the column to an array, and the pipeline writes objects
    into it. Anything else is ``_UNREADABLE``, at the level it went wrong."""
    if not isinstance(value, list):
        return _UNREADABLE
    return [read(t) if isinstance(t, dict) else _UNREADABLE for t in value]


def _totals(account: dict[str, Any], key: str) -> list[tuple[int, dict[str, Any]]]:
    """An account's readable totals of one kind, with their indexes."""
    totals = account[key]
    if totals is _UNREADABLE:
        return []
    return [(i, t) for i, t in enumerate(totals) if t is not _UNREADABLE]


def account_out(row: dict[str, Any]) -> dict[str, Any]:
    """One account in the contract's per-account shape."""
    return {
        "account_id": str(row["account_id"]),
        "ordinal": row["ordinal"],
        "match_key": {
            "account_type": row["account_type"],
            "last4": row["last4"],
            "occurrence": row["occurrence"],
        },
        "institution": row["institution"],
        "last4": row["last4"],
        "account_type": row["account_type"],
        "currency": row["currency"],
        "period_start": day(row["period_start"]),
        "period_end": day(row["period_end"]),
        "status": row["status"],
        "held_by_statement": row["held_by_statement"],
        "beginning_balance": money(row["beginning_balance"]),
        "ending_balance": money(row["ending_balance"]),
        "section_totals": _stored_totals(
            row["section_totals"],
            lambda t: {
                "section": _stored_field(t, "section"),
                "amount": _stored_money(_stored_field(t, "amount"), nullable=False),
            },
        ),
        "column_totals": _stored_totals(
            row["column_totals"],
            lambda t: {
                "section": _stored_field(t, "section"),
                "credits": _stored_money(_stored_field(t, "credits")),
                "debits": _stored_money(_stored_field(t, "debits")),
            },
        ),
        "line_order": row["line_order"],
        "line_count": row["line_count"],
        "date_grace_days": row["date_grace_days"],
        "failure_class": row["failure_class"],
        "difference": money(row["difference"]),
        "first_page": row["first_page"],
        "last_page": row["last_page"],
        "loan": _loan(row),
        "content_digest": bytes(row["content_digest"]).hex(),
    }


def account_text(account: dict[str, Any]) -> list[tuple[str, object]]:
    """An account's free-text fields, each named for the log."""
    where = f"accounts[{account['ordinal']}]"
    fields: list[tuple[str, object]] = [
        (f"{where}.institution", account["institution"])
    ]
    for i, total in _totals(account, "section_totals"):
        # Never null in a section total (the contract): a null one is unread.
        section = _UNREADABLE if total["section"] is None else total["section"]
        fields.append((f"{where}.section_totals[{i}].section", section))
    for i, total in _totals(account, "column_totals"):
        fields.append((f"{where}.column_totals[{i}].section", total["section"]))
    return fields


def servable(value: object) -> bool:
    """Whether a stored text field can be served: null, or text in bound."""
    if value is None:
        return True
    return isinstance(value, str) and within_mask_bound(value)


def account_unreadable(account: dict[str, Any]) -> list[str]:
    """The names of an account's stored totals that can't be served: a
    totals value that isn't a list, an element that isn't an object, or a
    figure that isn't contract money."""
    where = f"accounts[{account['ordinal']}]"
    failed: list[str] = []
    for key, sides in (
        ("section_totals", ("amount",)),
        ("column_totals", ("credits", "debits")),
    ):
        if account[key] is _UNREADABLE:
            failed.append(f"{where}.{key}")
            continue
        for i, total in enumerate(account[key]):
            if total is _UNREADABLE:
                failed.append(f"{where}.{key}[{i}]")
                continue
            failed.extend(
                f"{where}.{key}[{i}].{side}"
                for side in sides
                if total[side] is _UNREADABLE
            )
    return failed


def statement_out(
    rev: dict[str, Any], accounts: list[dict[str, Any]]
) -> dict[str, Any]:
    """One revision in the contract's per-statement shape."""
    return {
        "contract_version": STATEMENT_CONTRACT_VERSION,
        "statement_id": str(rev["statement_id"]),
        "source_sha256": bytes(rev["source_sha256"]).hex(),
        "revision": rev["revision"],
        "extracted_at": timestamp(rev["created_at"]),
        "is_current": rev["is_current"],
        "current_revision": rev["current_revision"],
        "document_type": rev["document_type"],
        "rejected_bound": rev["rejected_bound"],
        "status": rev["status"],
        "hold_reason": rev["hold_reason"],
        "period_start": day(rev["period_start"]),
        "period_end": day(rev["period_end"]),
        "printed_account_count": rev["printed_account_count"],
        "pages": rev["pages"],
        "extractor": {
            "model": rev["llm_model"],
            "prompt_version": rev["prompt_version"],
            "first_pass_version": rev["first_pass_version"],
        },
        "masked_runs": rev["masked_runs"],
        "accounts": accounts,
    }


def line_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "line_id": str(row["line_id"]),
        "ordinal": row["ordinal"],
        "posted_date": day(row["posted_date"]),
        "transaction_date": day(row["transaction_date"]),
        "description": row["description"],
        "amount": money(row["amount"]),
        "running_balance": money(row["running_balance"]),
        "section": row["section"],
        "page": row["page"],
    }


def change_out(change: StatementChange) -> dict[str, Any]:
    """One change-feed entry: a statement at its current revision."""
    return {
        "statement_id": str(change.statement_id),
        "revision": change.revision,
        "status": change.status,
        "document_type": change.document_type,
        "source_sha256": bytes(change.source_sha256).hex(),
    }


def statement_read_failures(accounts: list[dict[str, Any]]) -> list[str]:
    """The names of a revision's served fields that fail the read check.

    ``accounts`` are :func:`account_out` results. A name is returned for a
    stored total that can't be served as contract money (or isn't the
    shape the contract carries), and for each free-text field (institution,
    totals sections) that isn't text or null, or fails the mask's bound.
    Empty when every field can be served.
    """
    unreadable = [name for a in accounts for name in account_unreadable(a)]
    texts = [field for a in accounts for field in account_text(a)]
    return [*unreadable, *(name for name, value in texts if not servable(value))]


def lines_read_failures(lines: list[dict[str, Any]]) -> list[str]:
    """The names of a page of lines' text fields that fail the read check.

    ``lines`` are :func:`line_out` results.
    """
    return [
        f"lines[{line['ordinal']}].{name}"
        for line in lines
        for name in ("description", "section")
        if not servable(line[name])
    ]
