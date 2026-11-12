"""Tests for the statement gates (plan U4).

Every account here is synthetic: invented institutions, numbers and amounts.
"""

from __future__ import annotations

import hashlib
import json
import typing
from datetime import date
from typing import Any

import pytest
from pydantic import BaseModel, StringConstraints

from receipt_index.statement_extraction import (
    AccountExtraction,
    AccountRun,
    CallUsage,
    FirstPass,
    LoanExtraction,
    SectionTotal,
)
from receipt_index.statement_gates import (
    CARD_GRACE_DAYS,
    GatedAccount,
    GatedStatement,
    SectionTotalFigure,
    canonical_json,
    content_digest,
    gate_statement,
)

_JANUARY = {
    "start": {"month": 1, "day": 1, "year": 2027},
    "end": {"month": 1, "day": 31, "year": 2027},
}
_NUMBER = "4111 1111 1111 3456"


def _line(
    day: int,
    amount: str,
    balance: str | None = None,
    *,
    month: int = 1,
    year: int | None = None,
    section: str | None = None,
    description: str = "Synthetic purchase",
    page: int = 1,
) -> dict[str, Any]:
    return {
        "posted": {"month": month, "day": day, "year": year},
        "description": description,
        "amount": amount,
        "running_balance": balance,
        "section": section,
        "page": page,
    }


def _balance(
    lines: list[dict[str, Any]] | None = None,
    *,
    beginning: str | None = "100.00",
    ending: str | None = "100.00",
    period: dict[str, Any] | None = _JANUARY,
    **fields: Any,
) -> AccountRun:
    output = AccountExtraction.model_validate(
        {
            "institution": "Synthetic Savings Bank",
            "last4": "1234",
            "currency": "USD",
            "period": period,
            "beginning_balance": beginning,
            "ending_balance": ending,
            "line_order": "oldest_first",
            "lines": lines or [],
            **fields,
        }
    )
    return AccountRun(output=output, capped=False, usage=CallUsage())


def _clean() -> AccountRun:
    return _balance(
        [_line(5, "-10.00", "90.00"), _line(9, "25.00", "115.00")], ending="115.00"
    )


def _loan(**figures: Any) -> AccountRun:
    output = LoanExtraction.model_validate(
        {"institution": "Synthetic Mortgage Co", "last4": "9876", **figures}
    )
    return AccountRun(output=output, capped=False, usage=CallUsage())


_CAPPED = AccountRun(output=None, capped=True, usage=CallUsage())


def _gate(
    *accounts: tuple[str, AccountRun],
    document_type: str = "deposit",
    printed: int | None = None,
    period: dict[str, Any] | None = _JANUARY,
) -> GatedStatement:
    first = FirstPass.model_validate(
        {
            "document_type": document_type,
            "accounts": [
                {
                    "label": f"Account {n}",
                    "account_type": account_type,
                    "first_page": n,
                    "last_page": n + 1,
                }
                for n, (account_type, _) in enumerate(accounts, 1)
            ],
            "printed_account_count": printed,
            "period": period,
        }
    )
    return gate_statement(first, [run for _, run in accounts])


def _one(account_type: str, run: AccountRun, **kwargs: Any) -> GatedAccount:
    return _gate((account_type, run), **kwargs).accounts[0]


# ---------------------------------------------------------------------------
# A clean account
# ---------------------------------------------------------------------------


def test_a_clean_account_carries_its_lines_in_the_contracts_formats() -> None:
    statement = _gate(("deposit", _clean()))
    assert statement.status == "complete"
    assert statement.hold_reason is None
    account = statement.accounts[0]
    assert account.status == "clean"
    assert account.failure_class is None
    assert account.difference is None
    assert account.first_page is None
    assert account.line_count == 2
    assert account.period_start == date(2027, 1, 1)
    assert account.lines[0].posted_date == date(2027, 1, 5)
    assert account.lines[1].amount == "25.00"
    assert account.lines[1].running_balance == "115.00"
    assert account.date_grace_days == 0
    assert len(account.content_digest) == 64
    assert account.content_digest == content_digest(account)


def test_a_dormant_account_is_clean() -> None:
    account = _one("deposit", _balance([], beginning="50.00", ending="50.00"))
    assert account.status == "clean"
    assert account.line_count == 0


# ---------------------------------------------------------------------------
# One test per failure class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("account_type", "run", "failure"),
    [
        pytest.param(
            "deposit",
            _balance([], beginning="50.00", ending="60.00"),
            "empty_account",
            id="empty-not-dormant",
        ),
        pytest.param(
            "card",
            _balance([], beginning=None, ending=None),
            "empty_account",
            id="empty",
        ),
        pytest.param(
            "deposit",
            _balance([_line(5, "-10.00")], beginning=None, ending="90.00"),
            "no_beginning_balance",
            id="no-beginning",
        ),
        pytest.param(
            "deposit",
            _balance([_line(5, "-10.00")], ending=None),
            "no_ending_balance",
            id="no-ending",
        ),
        pytest.param("card", _CAPPED, "too_many_lines", id="capped"),
        pytest.param(
            "deposit",
            _balance([_line(5, "-10.00")], ending="90.00", period=None),
            "undated",
            id="undated",
        ),
        pytest.param(
            "deposit",
            _balance([_line(5, "-10.00")], ending="95.00"),
            "does_not_foot",
            id="does-not-foot",
        ),
        pytest.param(
            "deposit",
            _balance([_line(15, "-10.00", month=2, year=2027)], ending="90.00"),
            "date_out_of_period",
            id="date-after-period",
        ),
        pytest.param(
            "deposit",
            _balance([_line(30, "-10.00", month=2)], ending="90.00"),
            "date_out_of_period",
            id="date-that-doesnt-exist",
        ),
        pytest.param(
            "deposit",
            _balance([_line(5, "-10.00", "80.00")], ending="90.00"),
            "balance_chain_break",
            id="chain-break",
        ),
        pytest.param(
            "deposit",
            _balance(
                [_line(5, "-10.00", section="Withdrawals")],
                ending="90.00",
                section_totals=[{"section": "Withdrawals:", "amount": "12.00"}],
            ),
            "totals_mismatch",
            id="section-total",
        ),
        pytest.param(
            "deposit",
            _balance(
                [_line(5, "-10.00"), _line(6, "30.00")],
                ending="120.00",
                column_totals=[{"credits": "30.00", "debits": "11.00"}],
            ),
            "totals_mismatch",
            id="column-total",
        ),
    ],
)
def test_each_failure_class_holds_the_account(
    account_type: str, run: AccountRun, failure: str
) -> None:
    # An undated account has no period of its own or the document's.
    period = None if failure == "undated" else _JANUARY
    statement = _gate((account_type, run), ("deposit", _clean()), period=period)
    account = statement.accounts[0]
    assert account.status == "held"
    assert account.failure_class == failure
    # An empty account also holds the statement, as dropped_account.
    assert account.held_by_statement is (failure == "empty_account")
    assert account.lines == ()
    assert (account.first_page, account.last_page) == (1, 2)


@pytest.mark.parametrize(
    ("run", "failure"),
    [
        # No beginning balance comes before not footing.
        (
            _balance([_line(5, "-10.00")], beginning=None, ending="1.00"),
            "no_beginning_balance",
        ),
        # Not footing comes before a date out of the period.
        (_balance([_line(5, "-10.00", year=2030)], ending="1.00"), "does_not_foot"),
        # A date out of the period comes before a chain break.
        (
            _balance([_line(5, "-10.00", "1.00", year=2030)], ending="90.00"),
            "date_out_of_period",
        ),
        # A chain break comes before a totals mismatch.
        (
            _balance(
                [_line(5, "-10.00", "1.00", section="fees")],
                ending="90.00",
                section_totals=[{"section": "fees", "amount": "99.00"}],
            ),
            "balance_chain_break",
        ),
        # Undated comes before not footing.
        (_balance([_line(5, "-10.00")], ending="1.00", period=None), "undated"),
    ],
)
def test_an_account_carries_the_first_failure_that_applies(
    run: AccountRun, failure: str
) -> None:
    assert _gate(("deposit", run), period=None).accounts[0].failure_class == failure


def test_a_held_account_gives_its_difference_balances_and_totals() -> None:
    run = _balance(
        [_line(5, "-10.00", section="Fees")],
        ending="95.00",
        section_totals=[{"section": "Fees", "amount": "10.00"}],
    )
    account = _gate(("deposit", run), ("deposit", _clean())).accounts[0]
    assert account.failure_class == "does_not_foot"
    assert account.difference == "5.00"
    assert account.beginning_balance == "100.00"
    assert account.ending_balance == "95.00"
    assert account.section_totals == (SectionTotalFigure("fees", "10.00"),)
    assert account.lines == ()


def test_a_capped_account_has_no_figures_but_keeps_its_period() -> None:
    account = _gate(("card", _CAPPED), ("deposit", _clean())).accounts[0]
    assert account.failure_class == "too_many_lines"
    assert account.difference is None
    assert account.beginning_balance is None
    assert account.date_grace_days == CARD_GRACE_DAYS
    assert (account.period_start, account.period_end) == (
        date(2027, 1, 1),
        date(2027, 1, 31),
    )


def test_a_call_with_no_output_that_wasnt_capped_is_refused() -> None:
    uncapped = AccountRun(output=None, capped=False, usage=CallUsage())
    with pytest.raises(ValueError, match="no output"):
        _gate(("deposit", uncapped))


def test_an_account_passed_as_an_empty_extraction_drops_the_statement() -> None:
    # How U5 passes an account it doesn't call (its pages lie past the
    # document's): an empty extraction, gated as empty_account.
    empty = _balance([], beginning=None, ending=None)
    statement = _gate(("deposit", _clean()), ("card", empty))
    assert (statement.status, statement.hold_reason) == ("held", "dropped_account")
    assert statement.accounts[1].failure_class == "empty_account"
    assert all(a.held_by_statement for a in statement.accounts)


@pytest.mark.parametrize("heading", [":", "  ", "\uff1a"])
def test_a_total_whose_heading_is_blank_holds_the_account(heading: str) -> None:
    section = _balance(
        [_line(5, "-10.00", "90.00")],
        ending="90.00",
        section_totals=[{"section": heading, "amount": "10.00"}],
    )
    account = _gate(("deposit", section), ("deposit", _clean())).accounts[0]
    assert account.failure_class == "totals_mismatch"
    assert account.section_totals[0].section == ""
    column = _balance(
        [_line(5, "-10.00", "90.00")],
        ending="90.00",
        column_totals=[{"section": heading, "debits": "10.00"}],
    )
    account = _gate(("deposit", column), ("deposit", _clean())).accounts[0]
    assert account.failure_class == "totals_mismatch"


def test_a_column_total_with_neither_side_printed_checks_nothing() -> None:
    run = _balance(
        [_line(5, "-10.00", "90.00")], ending="90.00", column_totals=[{"section": "x"}]
    )
    assert _one("deposit", run).status == "clean"


def test_a_loans_masked_institution_counts_toward_masked_runs() -> None:
    statement = _gate(
        ("loan", _loan(institution=f"Synthetic {_NUMBER}", interest="1.00")),
        ("deposit", _masked_line_run()),
        document_type="loan_statement",
    )
    assert statement.status == "complete"
    assert statement.accounts[0].institution == "Synthetic 3456"
    assert statement.masked_runs == 2


def test_a_chain_carries_amounts_across_lines_with_no_printed_balance() -> None:
    run = _balance(
        [_line(5, "-10.00"), _line(6, "-5.00"), _line(7, "20.00", "105.00")],
        ending="105.00",
    )
    assert _one("deposit", run).status == "clean"


def test_a_newest_first_chain_passes_in_date_order_and_fails_in_printed_order() -> None:
    lines = [_line(9, "25.00", "115.00"), _line(5, "-10.00", "90.00")]
    newest = _balance(lines, ending="115.00", line_order="newest_first")
    assert _one("deposit", newest).status == "clean"
    printed_order = _balance(lines, ending="115.00", line_order="oldest_first")
    assert _one("deposit", printed_order).failure_class == "balance_chain_break"


def test_a_newest_first_accounts_lines_keep_their_printed_ordinals() -> None:
    lines = [_line(9, "25.00", "115.00"), _line(5, "-10.00", "90.00")]
    account = _one(
        "deposit", _balance(lines, ending="115.00", line_order="newest_first")
    )
    assert [line.ordinal for line in account.lines] == [1, 2]
    assert account.lines[0].posted_date == date(2027, 1, 9)


def test_section_totals_compare_magnitudes() -> None:
    run = _balance(
        [
            _line(5, "-10.00", section="Withdrawals"),
            _line(6, "-2.50", section="withdrawals"),
        ],
        ending="87.50",
        section_totals=[{"section": "WITHDRAWALS", "amount": "12.50"}],
    )
    assert _one("deposit", run).status == "clean"


@pytest.mark.parametrize(
    ("account_type", "credits", "debits"),
    [("deposit", "30.00", "10.00"), ("card", "10.00", "30.00")],
)
def test_column_totals_follow_each_account_types_sides(
    account_type: str, credits: str, debits: str
) -> None:
    # On a deposit a positive line is a credit; on a card, a negative one.
    run = _balance(
        [_line(5, "-10.00"), _line(6, "30.00")],
        ending="120.00",
        column_totals=[{"credits": credits, "debits": debits}],
    )
    assert _one(account_type, run).status == "clean"
    swapped = _balance(
        [_line(5, "-10.00"), _line(6, "30.00")],
        ending="120.00",
        column_totals=[{"credits": debits, "debits": credits}],
    )
    assert _one(account_type, swapped).failure_class == "totals_mismatch"


def test_a_column_total_for_a_section_counts_only_that_section() -> None:
    run = _balance(
        [_line(5, "-10.00", section="fees"), _line(6, "30.00", section="deposits")],
        ending="120.00",
        column_totals=[{"section": "Fees", "debits": "10.00"}],
    )
    assert _one("deposit", run).status == "clean"


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_DEC_JAN = {
    "start": {"month": 12, "day": 10, "year": None},
    "end": {"month": 1, "day": 9, "year": 2027},
}


def test_a_december_to_january_card_period_places_late_december_in_its_year() -> None:
    run = _balance([_line(28, "40.00", month=12), _line(3, "-40.00")], period=_DEC_JAN)
    account = _one("card", run)
    assert account.status == "clean"
    assert account.lines[0].posted_date == date(2026, 12, 28)
    assert account.lines[1].posted_date == date(2027, 1, 3)


def test_a_line_in_the_next_december_fails_the_date_gate() -> None:
    run = _balance(
        [_line(28, "40.00", month=12, year=2027), _line(3, "-40.00")], period=_DEC_JAN
    )
    assert _one("card", run).failure_class == "date_out_of_period"


@pytest.mark.parametrize(("day", "card"), [(3, "clean"), (2, "date_out_of_period")])
def test_a_card_has_seven_days_grace_and_a_deposit_none(day: int, card: str) -> None:
    run = _balance([_line(day, "40.00", month=12), _line(3, "-40.00")], period=_DEC_JAN)
    account = _one("card", run)
    assert (account.failure_class or account.status) == card
    deposit = _one("deposit", run)
    assert deposit.failure_class == "date_out_of_period"


def test_the_accounts_own_period_comes_before_the_documents() -> None:
    february = {
        "start": {"month": 2, "day": 1, "year": 2027},
        "end": {"month": 2, "day": 28, "year": 2027},
    }
    run = _balance([_line(5, "-10.00", month=2)], ending="90.00", period=february)
    account = _one("deposit", run)
    assert account.status == "clean"
    assert account.period_start == date(2027, 2, 1)


def test_an_account_with_no_period_of_its_own_takes_the_documents() -> None:
    account = _one(
        "deposit", _balance([_line(5, "-10.00")], ending="90.00", period=None)
    )
    assert account.status == "clean"
    assert account.period_end == date(2027, 1, 31)


def test_with_no_period_at_all_printed_years_pass_and_skip_the_date_gate() -> None:
    run = _balance([_line(5, "-10.00", year=2030)], ending="90.00", period=None)
    account = _gate(("deposit", run), period=None).accounts[0]
    assert account.status == "clean"
    assert account.period_start is None


def test_a_date_that_doesnt_exist_fails_even_with_no_period() -> None:
    # Otherwise its line would drop out of a clean account unseen.
    run = _balance(
        [_line(30, "-10.00", month=2, year=2027)], ending="90.00", period=None
    )
    account = _gate(("deposit", run), ("deposit", _clean()), period=None).accounts[0]
    assert account.failure_class == "date_out_of_period"


def test_a_transaction_date_is_placed_but_never_gated() -> None:
    line = {**_line(3, "40.00"), "transacted": {"month": 11, "day": 20, "year": None}}
    run = _balance([line, _line(4, "-40.00")])
    account = _one("card", run)
    assert account.status == "clean"
    assert account.lines[0].transaction_date == date(2026, 11, 20)


# ---------------------------------------------------------------------------
# Statement status and hold reasons
# ---------------------------------------------------------------------------


def test_a_loan_only_statement_is_complete() -> None:
    statement = _gate(
        ("loan", _loan(principal_after="150000.00", interest="600.00")),
        document_type="loan_statement",
    )
    assert statement.status == "complete"
    loan = statement.accounts[0]
    assert loan.status == "figures_only"
    assert loan.loan is not None
    assert loan.loan.interest == "600.00"
    assert loan.lines == ()


def test_a_loan_beside_a_held_deposit_account_is_incomplete() -> None:
    held = _balance([_line(5, "-10.00")], ending="95.00")
    statement = _gate(("loan", _loan(interest="600.00")), ("deposit", held))
    assert statement.status == "incomplete"
    assert statement.hold_reason is None
    assert [a.status for a in statement.accounts] == ["figures_only", "held"]


def test_a_statement_with_no_accounts_is_held() -> None:
    statement = _gate()
    assert (statement.status, statement.hold_reason) == ("held", "no_accounts")
    assert statement.accounts == ()


def test_an_empty_loan_holds_every_account_as_dropped() -> None:
    held = _balance([_line(5, "-10.00")], ending="95.00")
    statement = _gate(("deposit", _clean()), ("loan", _loan()), ("card", held))
    assert (statement.status, statement.hold_reason) == ("held", "dropped_account")
    clean, loan, card = statement.accounts
    assert all(a.status == "held" and a.held_by_statement for a in statement.accounts)
    assert clean.failure_class is None
    assert clean.lines == ()
    assert loan.failure_class == "empty_account"
    assert card.failure_class == "does_not_foot"
    assert card.difference == "5.00"


def test_a_printed_count_that_disagrees_holds_every_account() -> None:
    statement = _gate(("deposit", _clean()), printed=2)
    assert (statement.status, statement.hold_reason) == (
        "held",
        "account_count_mismatch",
    )
    assert statement.accounts[0].held_by_statement
    assert statement.accounts[0].lines == ()


def test_a_printed_count_that_agrees_is_no_hold() -> None:
    assert _gate(("deposit", _clean()), printed=1).status == "complete"


def test_no_usable_account_holds_the_statement_but_not_by_the_list() -> None:
    held = _balance([_line(5, "-10.00")], ending="95.00")
    statement = _gate(("deposit", held), ("card", _CAPPED))
    assert (statement.status, statement.hold_reason) == ("held", "no_usable_account")
    assert not any(a.held_by_statement for a in statement.accounts)
    assert [a.failure_class for a in statement.accounts] == [
        "does_not_foot",
        "too_many_lines",
    ]


def test_hold_reasons_apply_in_order() -> None:
    # An empty account and a wrong count: dropped_account comes first.
    statement = _gate(("deposit", _clean()), ("loan", _loan()), printed=5)
    assert statement.hold_reason == "dropped_account"
    # A wrong count and no usable account: the count comes first.
    held = _balance([_line(5, "-10.00")], ending="95.00")
    assert _gate(("deposit", held), printed=3).hold_reason == "account_count_mismatch"


@pytest.mark.parametrize(
    "document_type", ["loan_bill", "tax_or_escrow_notice", "not_statement"]
)
def test_a_rejected_type_gives_no_accounts_whatever_the_first_pass_listed(
    document_type: str,
) -> None:
    statement = _gate(("deposit", _clean()), document_type=document_type)
    assert statement == GatedStatement(status="rejected", hold_reason=None)


def test_runs_must_match_the_listed_accounts() -> None:
    first = FirstPass.model_validate(
        {
            "document_type": "deposit",
            "accounts": [
                {
                    "label": "x",
                    "account_type": "deposit",
                    "first_page": 1,
                    "last_page": 1,
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="account results"):
        gate_statement(first, [])


def test_an_output_of_the_wrong_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="loan"):
        _gate(("deposit", _loan(interest="1.00")))
    with pytest.raises(ValueError, match="loan"):
        _gate(("loan", _clean()))


# ---------------------------------------------------------------------------
# match_key and occurrence
# ---------------------------------------------------------------------------


def test_occurrence_counts_accounts_of_one_type_and_last4() -> None:
    statement = _gate(
        ("card", _clean()),
        ("card", _clean()),
        ("deposit", _clean()),
        document_type="card",
    )
    assert [a.match_key for a in statement.accounts] == [
        {"account_type": "card", "last4": "1234", "occurrence": 1},
        {"account_type": "card", "last4": "1234", "occurrence": 2},
        {"account_type": "deposit", "last4": "1234", "occurrence": 1},
    ]


def test_an_institutions_whitespace_changes_neither_match_key_nor_digest() -> None:
    plain = _one("deposit", _clean())
    lines = [_line(5, "-10.00", "90.00"), _line(9, "25.00", "115.00")]
    spaced = _one(
        "deposit",
        _balance(
            lines, ending="115.00", institution="  Synthetic \t Savings\u00a0Bank\n"
        ),
    )
    assert spaced.institution == "Synthetic Savings Bank"
    assert spaced.match_key == plain.match_key
    assert spaced.content_digest == plain.content_digest


# ---------------------------------------------------------------------------
# The canonical form and digest
# ---------------------------------------------------------------------------

# The worked example the contract document gives: a card account, one line,
# with a section total and a column total. Every key, in sorted order.
_WORKED_EXAMPLE = (
    '{"account_type":"card","beginning_balance":"-5.00",'
    '"column_totals":[{"credits":"5.00","debits":"42.50","section":null}],'
    '"currency":"USD","difference":null,"ending_balance":"32.50",'
    '"failure_class":null,"held_by_statement":false,'
    '"institution":"Synthetic Card Services","last4":"4321",'
    '"line_order":"oldest_first",'
    '"lines":[{"amount":"42.50","description":"Caf\u00e9 purchase 3456",'
    '"page":2,"posted_date":"2027-01-05","running_balance":null,'
    '"section":"purchases","transaction_date":"2027-01-03"},'
    '{"amount":"-5.00","description":"Payment received","page":2,'
    '"posted_date":"2027-01-09","running_balance":"32.50",'
    '"section":"payments and credits","transaction_date":null}],'
    '"loan":null,"period_end":"2027-01-31","period_start":"2027-01-01",'
    '"section_totals":[{"amount":"42.50","section":"purchases"}],'
    '"status":"clean"}'
)


def _worked_example() -> GatedAccount:
    run = _balance(
        [
            {
                **_line(5, "42.50", section="Purchases:", page=2),
                "transacted": {"month": 1, "day": 3, "year": None},
                "description": f"Caf\u00e9 purchase {_NUMBER}",
            },
            _line(
                9,
                "-5.00",
                "32.50",
                section="Payments and Credits",
                description="Payment received",
                page=2,
            ),
        ],
        beginning="-5.00",
        ending="32.50",
        institution="Synthetic Card Services",
        last4="4321",
        section_totals=[{"section": "Purchases", "amount": "42.50"}],
        column_totals=[{"credits": "5.00", "debits": "42.50"}],
    )
    return _one("card", run)


def test_the_canonical_form_matches_the_worked_example() -> None:
    account = _worked_example()
    assert canonical_json(account) == _WORKED_EXAMPLE.encode("utf-8")
    expected = hashlib.sha256(_WORKED_EXAMPLE.encode("utf-8")).hexdigest()
    assert account.content_digest == expected
    # The SHA-256 of the synthetic example above, pinned for the contract.
    worked_digest = (
        "4e05fb320609872d"  # pragma: allowlist secret
        "b5ec1e8d8211cc1f"  # pragma: allowlist secret
        "57dfb38a41a3cf66"  # pragma: allowlist secret
        "3db40c206e3f630d"  # pragma: allowlist secret
    )
    assert account.content_digest == worked_digest


def test_the_canonical_form_keeps_utf8_and_has_no_whitespace() -> None:
    raw = canonical_json(_worked_example())
    assert "\u00e9".encode() in raw
    assert b"\\u" not in raw
    assert b" :" not in raw
    assert b", " not in raw
    assert json.loads(raw)["lines"][0]["page"] == 2


def test_the_digest_follows_content_but_not_ordinals() -> None:
    base = _one("deposit", _clean())
    lines = [
        _line(5, "-10.00", "90.00"),
        _line(9, "25.00", "115.00", description="Other"),
    ]
    changed = _one("deposit", _balance(lines, ending="115.00"))
    assert changed.content_digest != base.content_digest
    second = _gate(("deposit", _dormant_other()), ("deposit", _clean())).accounts[1]
    assert second.ordinal == 2
    assert second.content_digest == base.content_digest


def _dormant_other() -> AccountRun:
    return _balance([], beginning="1.00", ending="1.00", last4="0001")


def test_a_held_by_statement_account_has_a_different_digest() -> None:
    clean = _one("deposit", _clean())
    held = _gate(("deposit", _clean()), printed=2).accounts[0]
    assert held.content_digest != clean.content_digest
    assert held.content_digest == content_digest(held)


def test_a_total_that_isnt_a_finite_amount_is_refused() -> None:
    output = AccountExtraction.model_validate(
        {
            "line_order": "oldest_first",
            "beginning_balance": "1.00",
            "ending_balance": "1.00",
        }
    )
    for bad in ("NaN", "Infinity", "-Infinity", "sNaN"):
        output.section_totals = [SectionTotal.model_construct(section="x", amount=bad)]
        run = AccountRun(output=output, capped=False, usage=CallUsage())
        with pytest.raises(ValueError, match="finite"):
            _gate(("deposit", run))


# ---------------------------------------------------------------------------
# The mask
# ---------------------------------------------------------------------------


def test_the_mask_runs_before_the_digest_and_is_counted() -> None:
    account = _worked_example()
    assert account.lines[0].description == "Caf\u00e9 purchase 3456"
    assert _gate(("card", _masked_line_run())).masked_runs == 1


def _masked_line_run() -> AccountRun:
    run = _balance(
        [_line(5, "-10.00", "90.00", description=f"Card {_NUMBER}")], ending="90.00"
    )
    return run


def test_masked_runs_count_only_the_text_the_revision_stores() -> None:
    # A held account's lines aren't stored, so their runs aren't counted;
    # its institution and totals' sections are.
    held = _balance(
        [_line(5, "-10.00", description=f"Card {_NUMBER}")],
        ending="95.00",
        institution=f"Synthetic Bank {_NUMBER}",
        section_totals=[{"section": f"Card {_NUMBER}", "amount": "10.00"}],
    )
    statement = _gate(("deposit", held), ("deposit", _masked_line_run()))
    assert statement.status == "incomplete"
    assert statement.accounts[0].institution == "Synthetic Bank 3456"
    assert statement.accounts[0].section_totals[0].section == "card 3456"
    assert statement.masked_runs == 3


def test_a_statement_held_by_its_list_counts_no_line_text() -> None:
    statement = _gate(("deposit", _masked_line_run()), printed=2)
    assert statement.masked_runs == 0


# Each free-text field of the account outputs, and how the test puts a
# number in it. A new text field fails this test until it is masked and
# added here.
_FREE_TEXT = {
    (AccountExtraction, "institution"): "institution",
    (AccountExtraction, "lines.description"): "description",
    (AccountExtraction, "lines.section"): "section",
    (AccountExtraction, "section_totals.section"): "section",
    (AccountExtraction, "column_totals.section"): "section",
    (LoanExtraction, "institution"): "institution",
}


def _free_text_fields(model: type[BaseModel], prefix: str = "") -> list[str]:
    """Every str field of ``model``, nested models included, with no pattern."""
    found = []
    hints = typing.get_type_hints(model, include_extras=True)
    for name in model.model_fields:
        found.extend(_text_paths(hints[name], f"{prefix}{name}"))
    return found


def _text_paths(hint: Any, path: str) -> list[str]:
    origin = typing.get_origin(hint)
    if origin is typing.Annotated:
        base, *meta = typing.get_args(hint)
        if base is str:
            patterned = any(
                isinstance(m, StringConstraints) and m.pattern for m in meta
            )
            return [] if patterned else [path]
        return _text_paths(base, path)
    if hint is str:
        return [path]
    if isinstance(hint, type) and issubclass(hint, BaseModel):
        return _free_text_fields(hint, f"{path}.")
    paths = []
    for arg in typing.get_args(hint):
        if arg is not type(None):
            paths.extend(_text_paths(arg, path))
    return paths


def _with_number(knob: str, account_type: str) -> AccountRun:
    if account_type == "loan":
        return _loan(institution=f"Synthetic {_NUMBER}", interest="1.00")
    section = f"Fees {_NUMBER}" if knob == "section" else "Fees"
    description = f"Fee {_NUMBER}" if knob == "description" else "Fee"
    institution = f"Synthetic {_NUMBER}" if knob == "institution" else "Synthetic"
    return _balance(
        [_line(5, "-10.00", "90.00", section=section, description=description)],
        ending="90.00",
        institution=institution,
        section_totals=[{"section": section, "amount": "10.00"}],
        column_totals=[{"section": section, "debits": "10.00"}],
    )


@pytest.mark.parametrize("model", [AccountExtraction, LoanExtraction])
def test_every_free_text_field_is_masked(model: type[BaseModel]) -> None:
    paths = _free_text_fields(model)
    assert paths, "the walk found no text fields"
    for path in paths:
        knob = _FREE_TEXT[(model, path)]  # a new field must be added above
        account_type = "loan" if model is LoanExtraction else "deposit"
        statement = _gate(
            (account_type, _with_number(knob, account_type)),
            document_type="loan_statement" if model is LoanExtraction else "deposit",
        )
        account = statement.accounts[0]
        assert account.status in ("clean", "figures_only"), path
        raw = canonical_json(account).decode("utf-8")
        digits = _NUMBER.replace(" ", "")
        assert digits not in raw.replace(" ", ""), path
        assert "3456" in raw, path
        assert statement.masked_runs >= 1, path
