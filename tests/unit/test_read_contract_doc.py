"""The read contract's worked digest example matches the code that writes digests."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from receipt_index.statement_gates import (
    ColumnTotalFigure,
    GatedAccount,
    GatedLine,
    LoanFigures,
    SectionTotalFigure,
    canonical_form,
    canonical_json,
    content_digest,
)

_DOC = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "engineering"
    / "designs"
    / "statement-read-contract.md"
)


def _block(text: str, marker: str) -> str:
    """The fenced block straight after ``<!-- digest-example:<marker> -->``."""
    found = re.search(
        rf"<!-- digest-example:{marker} -->\n```[a-z]*\n(.*?)\n```", text, re.DOTALL
    )
    assert found, f"no digest-example:{marker} block in the contract"
    return found.group(1)


def _day(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _account(form: dict[str, Any]) -> GatedAccount:
    lines = tuple(
        GatedLine(
            ordinal=index,
            posted_date=date.fromisoformat(line["posted_date"]),
            transaction_date=_day(line["transaction_date"]),
            description=line["description"],
            amount=line["amount"],
            running_balance=line["running_balance"],
            section=line["section"],
            page=line["page"],
        )
        for index, line in enumerate(form["lines"], start=1)
    )
    return GatedAccount(
        ordinal=1,
        occurrence=1,
        account_type=form["account_type"],
        status=form["status"],
        held_by_statement=form["held_by_statement"],
        failure_class=form["failure_class"],
        institution=form["institution"],
        last4=form["last4"],
        currency=form["currency"],
        period_start=_day(form["period_start"]),
        period_end=_day(form["period_end"]),
        beginning_balance=form["beginning_balance"],
        ending_balance=form["ending_balance"],
        difference=form["difference"],
        first_page=None,
        last_page=None,
        line_order=form["line_order"],
        date_grace_days=7,
        section_totals=tuple(SectionTotalFigure(**t) for t in form["section_totals"]),
        column_totals=tuple(ColumnTotalFigure(**t) for t in form["column_totals"]),
        loan=None if form["loan"] is None else LoanFigures(**form["loan"]),
        lines=lines,
        content_digest="",
    )


def test_the_worked_example_digest_is_what_the_gates_compute() -> None:
    text = _DOC.read_text(encoding="utf-8")
    form = json.loads(_block(text, "form"))
    account = _account(form)

    assert canonical_form(account) == form
    assert content_digest(account) == _block(text, "digest").strip()


def test_the_documented_serialisation_is_the_gates_one() -> None:
    form = json.loads(_block(_DOC.read_text(encoding="utf-8"), "form"))
    documented = json.dumps(
        form, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    assert documented == canonical_json(_account(form))
