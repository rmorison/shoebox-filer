"""Statement gates: from the model's reading to statuses and digests (plan U4).

Pure functions. :func:`gate_statement` takes the first pass and each account
call's result, and returns the statement as the read contract gives it: each
account in the contract's shape, with its status, failure class and
``content_digest``, its lines only when it is clean, and the statement's
status, hold reason and ``masked_runs``.

Before any gate, every account's text goes through the steps the contract
names: the institution and section headings are normalised, and every
free-text field (a line's description and section, the totals' sections and
the institution) is masked, so no run of eight or more digits survives.
Money is put in the contract's format and dates are placed in their years.

The gates run in the contract's order and an account carries the first that
applies. Each is one the ledger can run again from the contract, so it
doesn't have to trust the status.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal

from receipt_index.statement_extraction import (
    AccountExtraction,
    AccountRun,
    FirstPass,
    ListedAccount,
    LoanExtraction,
    PrintedPeriod,
    format_money,
    mask_digits,
    normalise_institution,
    normalise_section,
    period_dates,
    place_date,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Days before the period a card's posted date may fall and still pass.
CARD_GRACE_DAYS: Final = 7

AccountStatus = Literal["clean", "held", "figures_only"]
StatementStatus = Literal["complete", "incomplete", "held", "rejected"]
FailureClass = Literal[
    "empty_account",
    "no_beginning_balance",
    "no_ending_balance",
    "too_many_lines",
    "undated",
    "does_not_foot",
    "date_out_of_period",
    "balance_chain_break",
    "totals_mismatch",
]
HoldReason = Literal[
    "no_accounts", "dropped_account", "account_count_mismatch", "no_usable_account"
]

# The document types the first pass may give that are extracted; any other
# is a rejection, whatever accounts the first pass listed.
STATEMENT_TYPES: Final = frozenset({"deposit", "card", "loan_statement"})
# The hold reasons that mean the account list itself can't be trusted, so
# every account is held by the statement.
_LIST_HOLDS: Final = frozenset(
    {"no_accounts", "dropped_account", "account_count_mismatch"}
)
_USABLE: Final = frozenset({"clean", "figures_only"})


# ---------------------------------------------------------------------------
# The contract's shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatedLine:
    """One line of a clean account, in the contract's formats."""

    ordinal: int
    posted_date: date
    transaction_date: date | None
    description: str
    amount: str
    running_balance: str | None
    section: str | None
    page: int


@dataclass(frozen=True)
class SectionTotalFigure:
    """A printed section total: the heading's key and the magnitude."""

    section: str
    amount: str


@dataclass(frozen=True)
class ColumnTotalFigure:
    """A table's printed column totals; ``section`` None is the whole account."""

    section: str | None
    credits: str | None
    debits: str | None


@dataclass(frozen=True)
class LoanFigures:
    """A loan's printed figures, in the contract's formats."""

    principal_before: str | None
    principal_after: str | None
    principal_paid: str | None
    interest: str | None
    escrow: str | None
    fees: str | None
    advances: str | None


@dataclass(frozen=True)
class GatedAccount:
    """One account of a revision, as the read contract gives it."""

    ordinal: int
    occurrence: int
    account_type: str
    status: AccountStatus
    held_by_statement: bool
    failure_class: FailureClass | None
    institution: str | None
    last4: str | None
    currency: str | None
    period_start: date | None
    period_end: date | None
    beginning_balance: str | None
    ending_balance: str | None
    difference: str | None
    first_page: int | None
    last_page: int | None
    line_order: str | None
    date_grace_days: int
    section_totals: tuple[SectionTotalFigure, ...]
    column_totals: tuple[ColumnTotalFigure, ...]
    loan: LoanFigures | None
    lines: tuple[GatedLine, ...]
    content_digest: str

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def match_key(self) -> dict[str, Any]:
        """Pairs this account with itself in another revision."""
        return {
            "account_type": self.account_type,
            "last4": self.last4,
            "occurrence": self.occurrence,
        }


@dataclass(frozen=True)
class GatedStatement:
    """A statement revision's status, hold reason and accounts."""

    status: StatementStatus
    hold_reason: HoldReason | None
    accounts: tuple[GatedAccount, ...] = ()
    masked_runs: int = 0


# ---------------------------------------------------------------------------
# Reading one account: masking, formats and dates
# ---------------------------------------------------------------------------


class _Masker:
    """Masks text and counts the runs it cut."""

    def __init__(self) -> None:
        self.runs = 0

    def text(self, value: str | None) -> str | None:
        masked, runs = mask_digits(value)
        self.runs += runs
        return masked

    def section(self, heading: str | None) -> str | None:
        # Normalised before the mask, so the mask sees the folded text, and
        # after it, since a cut run can leave a trailing colon or a space.
        return normalise_section(self.text(normalise_section(heading)))


@dataclass(frozen=True)
class _Line:
    """A line before its gates: its posted date may not be placeable."""

    posted: date | None
    # No year printed and no period to place it, as against a date that
    # doesn't exist.
    undated: bool
    line: GatedLine | None
    amount: Decimal
    running_balance: Decimal | None
    section: str | None


@dataclass
class _Read:
    """One account read into the contract's formats, before its gates."""

    institution: str | None = None
    last4: str | None = None
    currency: str | None = None
    period: tuple[date, date] | None = None
    beginning: str | None = None
    ending: str | None = None
    line_order: str | None = None
    section_totals: tuple[SectionTotalFigure, ...] = ()
    column_totals: tuple[ColumnTotalFigure, ...] = ()
    loan: LoanFigures | None = None
    lines: list[_Line] = field(default_factory=list)
    # A total whose printed heading normalises and masks to nothing: it
    # can't be matched to lines, so the account fails its totals.
    blank_heading: bool = False
    # Runs cut in text the contract always carries (institution, totals'
    # sections), and in the lines' text, carried only for a clean account.
    header_runs: int = 0
    line_runs: int = 0


def _money(value: str | None) -> str | None:
    return None if value is None else format_money(value)


def _period(*periods: PrintedPeriod | None) -> tuple[date, date] | None:
    """The first of ``periods`` that can be placed."""
    for period in periods:
        placed = period_dates(period)
        if placed is not None:
            return placed
    return None


def _read_balance_account(
    output: AccountExtraction,
    listed: ListedAccount,
    document_period: PrintedPeriod | None,
    grace_days: int,
) -> _Read:
    header = _Masker()
    read = _Read(
        institution=header.text(normalise_institution(output.institution)),
        last4=output.last4,
        currency=output.currency,
        period=_period(output.period, listed.period, document_period),
        beginning=_money(output.beginning_balance),
        ending=_money(output.ending_balance),
        line_order=output.line_order,
    )
    section_totals = []
    for printed_total in output.section_totals:
        section = header.section(printed_total.section)
        read.blank_heading |= section is None
        # A section total's section is never null in the contract.
        section_totals.append(
            SectionTotalFigure(section or "", format_money(printed_total.amount))
        )
    read.section_totals = tuple(section_totals)
    column_totals = []
    for printed_column in output.column_totals:
        section = header.section(printed_column.section)
        read.blank_heading |= printed_column.section is not None and section is None
        column_totals.append(
            ColumnTotalFigure(
                section, _money(printed_column.credits), _money(printed_column.debits)
            )
        )
    read.column_totals = tuple(column_totals)
    body = _Masker()
    for ordinal, printed in enumerate(output.lines, 1):
        posted = place_date(printed.posted, read.period, grace_days)
        transacted = (
            None
            if printed.transacted is None
            else place_date(printed.transacted, read.period, grace_days, gated=False)
        )
        section = body.section(printed.section)
        description = body.text(printed.description) or ""
        amount = format_money(printed.amount)
        running = _money(printed.running_balance)
        read.lines.append(
            _Line(
                posted=posted,
                undated=printed.posted.year is None and read.period is None,
                line=None
                if posted is None
                else GatedLine(
                    ordinal=ordinal,
                    posted_date=posted,
                    transaction_date=transacted,
                    description=description,
                    amount=amount,
                    running_balance=running,
                    section=section,
                    page=printed.page,
                ),
                amount=Decimal(amount),
                running_balance=None if running is None else Decimal(running),
                section=section,
            )
        )
    read.header_runs, read.line_runs = header.runs, body.runs
    return read


def _read_loan(
    output: LoanExtraction, listed: ListedAccount, document_period: PrintedPeriod | None
) -> _Read:
    header = _Masker()
    read = _Read(
        institution=header.text(normalise_institution(output.institution)),
        last4=output.last4,
        currency=output.currency,
        period=_period(output.period, listed.period, document_period),
        loan=LoanFigures(
            principal_before=_money(output.principal_before),
            principal_after=_money(output.principal_after),
            principal_paid=_money(output.principal_paid),
            interest=_money(output.interest),
            escrow=_money(output.escrow),
            fees=_money(output.fees),
            advances=_money(output.advances),
        ),
    )
    read.header_runs = header.runs
    return read


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def _sum(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0))


def _difference(read: _Read) -> Decimal | None:
    """Ending less (beginning plus lines), where both balances are printed."""
    if read.beginning is None or read.ending is None:
        return None
    lines = _sum([line.amount for line in read.lines])
    return Decimal(read.ending) - (Decimal(read.beginning) + lines)


def _dates_in_period(read: _Read, grace_days: int) -> bool:
    # A posted date that doesn't exist fails, with or without a period;
    # otherwise its line would drop out of a clean account unseen. (A
    # yearless date with no period has already held the account as undated.)
    if any(line.posted is None for line in read.lines):
        return False
    if read.period is None:
        return True  # nothing to check the placed dates against
    start, end = read.period
    earliest = start - timedelta(days=grace_days)
    return all(
        line.posted is not None and earliest <= line.posted <= end
        for line in read.lines
    )


def _chain_holds(read: _Read) -> bool:
    """Each printed running balance follows, walked in date order.

    Oldest first is ascending ordinal; newest first, descending. A line
    with no printed balance carries its amount to the next that has one.
    """
    assert read.beginning is not None
    walk = read.lines[::-1] if read.line_order == "newest_first" else read.lines
    balance = Decimal(read.beginning)
    for line in walk:
        balance += line.amount
        if line.running_balance is not None and line.running_balance != balance:
            return False
    return True


def _is_credit(account_type: str, amount: Decimal) -> bool:
    """A line that raises a deposit balance or lowers a card balance."""
    return amount > 0 if account_type == "deposit" else amount < 0


def _totals_match(read: _Read, account_type: str) -> bool:
    if read.blank_heading:
        return False
    for total in read.section_totals:
        lines = [x.amount for x in read.lines if x.section == total.section]
        if abs(_sum(lines)) != Decimal(total.amount):
            return False
    for column in read.column_totals:
        scope = [
            x.amount
            for x in read.lines
            if column.section is None or x.section == column.section
        ]
        credits = _sum([abs(a) for a in scope if _is_credit(account_type, a)])
        debits = _sum([abs(a) for a in scope if not _is_credit(account_type, a)])
        if column.credits is not None and credits != Decimal(column.credits):
            return False
        if column.debits is not None and debits != Decimal(column.debits):
            return False
    return True


def _balance_failure(
    read: _Read, account_type: str, grace_days: int
) -> FailureClass | None:
    """The first gate a deposit or card account fails, or None when clean."""
    if not read.lines:
        dormant = read.beginning is not None and read.beginning == read.ending
        if not dormant:
            return "empty_account"
    if read.beginning is None:
        return "no_beginning_balance"
    if read.ending is None:
        return "no_ending_balance"
    if any(line.undated for line in read.lines):
        return "undated"
    if _difference(read) != 0:
        return "does_not_foot"
    if not _dates_in_period(read, grace_days):
        return "date_out_of_period"
    if not _chain_holds(read):
        return "balance_chain_break"
    if not _totals_match(read, account_type):
        return "totals_mismatch"
    return None


def _loan_failure(read: _Read) -> FailureClass | None:
    """A loan is held only when it prints no figure at all."""
    assert read.loan is not None
    printed = [value for value in asdict(read.loan).values() if value is not None]
    return None if printed else "empty_account"


# ---------------------------------------------------------------------------
# The canonical form and digest
# ---------------------------------------------------------------------------


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def canonical_form(account: GatedAccount) -> dict[str, Any]:
    """The account's canonical form for ``content_digest`` (contract v1)."""
    return {
        "account_type": account.account_type,
        "institution": account.institution,
        "last4": account.last4,
        "currency": account.currency,
        "period_start": _iso(account.period_start),
        "period_end": _iso(account.period_end),
        "status": account.status,
        "held_by_statement": account.held_by_statement,
        "failure_class": account.failure_class,
        "difference": account.difference,
        "beginning_balance": account.beginning_balance,
        "ending_balance": account.ending_balance,
        "section_totals": [asdict(t) for t in account.section_totals],
        "column_totals": [asdict(t) for t in account.column_totals],
        "line_order": account.line_order,
        "loan": None if account.loan is None else asdict(account.loan),
        "lines": [
            {
                "posted_date": _iso(line.posted_date),
                "transaction_date": _iso(line.transaction_date),
                "description": line.description,
                "amount": line.amount,
                "running_balance": line.running_balance,
                "section": line.section,
                "page": line.page,
            }
            for line in sorted(account.lines, key=lambda x: x.ordinal)
        ],
    }


def canonical_json(account: GatedAccount) -> bytes:
    """The canonical form as bytes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(
        canonical_form(account),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_digest(account: GatedAccount) -> str:
    """The lower-case hex SHA-256 of the account's canonical form."""
    return hashlib.sha256(canonical_json(account)).hexdigest()


# ---------------------------------------------------------------------------
# A statement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Gated:
    account: GatedAccount
    header_runs: int
    line_runs: int


def _gate_account(
    ordinal: int,
    listed: ListedAccount,
    run: AccountRun,
    document_period: PrintedPeriod | None,
) -> _Gated:
    """One account gated on its own."""
    is_loan = listed.account_type == "loan"
    grace = CARD_GRACE_DAYS if listed.account_type == "card" else 0
    failure: FailureClass | None
    if run.capped:
        # Nothing was read, but the period is known from the first pass.
        read = _Read(period=_period(listed.period, document_period))
        failure = "too_many_lines"
    elif run.output is None:
        # Only a capped call has no output. An account that isn't called
        # (U5: its pages lie past the document's) is passed as an empty
        # extraction, which gates as empty_account.
        msg = "an account call with no output that wasn't capped"
        raise ValueError(msg)
    elif isinstance(run.output, LoanExtraction):
        if not is_loan:
            msg = f"a loan's figures for a {listed.account_type} account"
            raise ValueError(msg)
        read = _read_loan(run.output, listed, document_period)
        failure = _loan_failure(read)
    else:
        if is_loan:
            msg = "an account's lines for a loan"
            raise ValueError(msg)
        read = _read_balance_account(run.output, listed, document_period, grace)
        failure = _balance_failure(read, listed.account_type, grace)

    own: AccountStatus = "figures_only" if is_loan else "clean"
    status: AccountStatus = "held" if failure is not None else own
    difference = _difference(read) if failure is not None else None
    lines = tuple(x.line for x in read.lines if x.line is not None)
    account = GatedAccount(
        ordinal=ordinal,
        occurrence=0,  # set across the statement
        account_type=listed.account_type,
        status=status,
        held_by_statement=False,
        failure_class=failure,
        institution=read.institution,
        last4=read.last4,
        currency=read.currency,
        period_start=None if read.period is None else read.period[0],
        period_end=None if read.period is None else read.period[1],
        beginning_balance=read.beginning,
        ending_balance=read.ending,
        difference=None if difference is None else format_money(str(difference)),
        first_page=listed.first_page if failure is not None else None,
        last_page=listed.last_page if failure is not None else None,
        line_order=read.line_order,
        date_grace_days=grace,
        section_totals=read.section_totals,
        column_totals=read.column_totals,
        loan=read.loan,
        lines=lines if status == "clean" else (),
        content_digest="",
    )
    return _Gated(account, read.header_runs, read.line_runs)


def _hold_reason(
    first_pass: FirstPass, accounts: Sequence[GatedAccount]
) -> HoldReason | None:
    if not accounts:
        return "no_accounts"
    if any(account.failure_class == "empty_account" for account in accounts):
        return "dropped_account"
    printed = first_pass.printed_account_count
    if printed is not None and printed != len(accounts):
        return "account_count_mismatch"
    if not any(account.status in _USABLE for account in accounts):
        return "no_usable_account"
    return None


def gate_statement(first_pass: FirstPass, runs: Sequence[AccountRun]) -> GatedStatement:
    """A statement's status, hold reason and accounts, from its calls.

    ``runs`` holds one account call's result per account the first pass
    listed, in its order. A rejected document type gives a rejected
    statement with no accounts, whatever the first pass listed.
    """
    if first_pass.document_type not in STATEMENT_TYPES:
        return GatedStatement(status="rejected", hold_reason=None)
    if len(runs) != len(first_pass.accounts):
        msg = f"{len(runs)} account results for {len(first_pass.accounts)} accounts"
        raise ValueError(msg)

    gated: list[_Gated] = []
    seen: dict[tuple[str, str | None], int] = {}
    pairs = zip(first_pass.accounts, runs, strict=True)
    for ordinal, (listed, run) in enumerate(pairs, 1):
        one = _gate_account(ordinal, listed, run, first_pass.period)
        key = (one.account.account_type, one.account.last4)
        seen[key] = seen.get(key, 0) + 1
        gated.append(replace(one, account=replace(one.account, occurrence=seen[key])))

    accounts = [one.account for one in gated]
    reason = _hold_reason(first_pass, accounts)
    if reason in _LIST_HOLDS:
        accounts = [
            replace(account, status="held", held_by_statement=True, lines=())
            for account in accounts
        ]
    status: StatementStatus
    if reason is not None:
        status = "held"
    elif all(account.status in _USABLE for account in accounts):
        status = "complete"
    else:
        status = "incomplete"
    # masked_runs counts the text the revision stores: every account's
    # institution and totals' sections, and the lines of the clean ones.
    masked_runs = sum(
        one.header_runs + (one.line_runs if account.lines else 0)
        for one, account in zip(gated, accounts, strict=True)
    )
    return GatedStatement(
        status=status,
        hold_reason=reason,
        accounts=tuple(
            replace(account, content_digest=content_digest(account))
            for account in accounts
        ),
        masked_runs=masked_runs,
    )
