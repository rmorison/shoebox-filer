"""The statement pipeline: one statement item from its bytes to a revision (plan U5).

:func:`process_statement` runs one Drive statement item that ``serve``'s
intake or the retry pass has fetched and logged as ``started``:

1. If a statement already exists for the same bytes, record this item as one
   more source of it and stop: no model call.
2. Store the original, write-once, under ``originals/``.
3. Read the PDF under ``STATEMENT_BOUNDS``. A scan, an unreadable file or one
   over a bound is a ``rejected`` revision; a read that timed out or failed
   is an item error, retried a bounded number of times.
4. The first pass. A document type that isn't extracted, more than
   :data:`MAX_ACCOUNTS` accounts or a capped first pass is a ``rejected``
   revision.
5. One call per account, then the gates (U4).
6. One short transaction: the original's record, the revision, accounts,
   clean accounts' lines, the statement, its source row and the ``success``
   row of ``ingest_log``.

Every PDF read and model call happens before the transaction starts, since
the feed lock it takes makes every other statement writer wait.

Nothing here logs or records model output or statement text: a failed call
is a :class:`~receipt_index.statement_extraction.StatementCallError`, which
carries none, and every other error is recorded by its type (see
:func:`_safe_message`).
"""

from __future__ import annotations

import hashlib
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import BuiltinMethodType
from typing import TYPE_CHECKING, Any, Final

import psycopg

from receipt_index.errors import classify
from receipt_index.fetching import STATEMENT_INGEST_TYPE
from receipt_index.originals import put_original
from receipt_index.pdf_reader import StatementRead, read_statement
from receipt_index.pdf_validate import STATEMENT_BOUNDS
from receipt_index.pipeline import ItemOutcome, RunControl
from receipt_index.repository import insert_ingest_log, record_original
from receipt_index.retries import AttemptState, RetryPolicy, failure_fields
from receipt_index.statement_extraction import (
    FIRST_PASS_VERSION,
    STATEMENT_PROMPT_VERSION,
    AccountExtraction,
    AccountRun,
    AccountType,
    CallUsage,
    FirstPass,
    LoanExtraction,
    StatementCallError,
    create_account_agent,
    create_first_pass_agent,
    mask_digits,
    period_dates,
    run_account,
    run_first_pass,
)
from receipt_index.statement_gates import (
    STATEMENT_TYPES,
    GatedAccount,
    GatedStatement,
    gate_statement,
)
from receipt_index.statement_repository import (
    AccountRow,
    LineRow,
    RevisionRow,
    add_source,
    create_statement,
    insert_account,
    insert_lines,
    insert_revision,
    new_statement_id,
    statement_for_sha256,
)
from receipt_index.store import StoreFolder

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from uuid import UUID

    from pydantic_ai import Agent
    from pydantic_ai.models import Model
    from pydantic_ai.settings import ModelSettings

    from receipt_index.errors import ErrorClass
    from receipt_index.models import RawReceipt
    from receipt_index.store import EvidenceStore

logger = logging.getLogger(__name__)

# The source type of a statement item in ingest_log, apart from receipts.
STATEMENT_SOURCE_TYPE: Final = STATEMENT_INGEST_TYPE

# The most accounts a statement may list; more is rejected as over_bounds,
# accounts. Each account is its own watchdog step and model call.
MAX_ACCOUNTS: Final = 8

_PDF: Final = "application/pdf"
_ACCOUNT_TYPES: Final[tuple[AccountType, ...]] = ("deposit", "card", "loan")

# read_statement's reasons that reject the statement, and as what.
_REJECTED_TYPES: Final = {"scan": "scan", "unreadable": "unreadable"}
_REJECTED_BOUNDS: Final = frozenset({"pages", "size", "memory", "text", "crash"})


# Tracing instrumentation patches the client, its transport, pydantic-ai or
# pydantic and exports what passes through: the statement's text and the
# model's output. `logfire` itself is no sign, since pydantic-ai's logfire
# extra imports it in every process. What is:
#
# - any OpenTelemetry instrumentor (a module under the package below);
# - any logfire integration module outside the baseline: each of logfire's
#   instrument_* calls loads its own (httpx, anthropic, openai, mcp,
#   pydantic_ai, ...), and the check fails closed on one it doesn't know.
#   The baseline is what the whole app loads at the pinned logfire, with no
#   instrument call; a test runs the real entry point and fails if a logfire
#   upgrade widens it, and another runs each instrument call for real.
_INSTRUMENTATION_PACKAGE: Final = "opentelemetry.instrumentation"
_LOGFIRE_INTEGRATIONS: Final = (
    "logfire._internal.integrations",
    "logfire.integrations",
)
_LOGFIRE_BASELINE: Final = frozenset(
    {
        "logfire._internal.integrations",
        "logfire._internal.integrations.executors",
        "logfire._internal.integrations.llm_providers",
        "logfire._internal.integrations.llm_providers.semconv",
        "logfire.integrations",
        "logfire.integrations.logging",
        "logfire.integrations.pydantic",
        "logfire.integrations.structlog",
    }
)


# logfire's pydantic plugin, whose setting says whether validations record.
_LOGFIRE_PYDANTIC_PLUGIN: Final = "logfire.integrations.pydantic"


class InstrumentationLoadedError(RuntimeError):
    """Tracing instrumentation is loaded: statement work is refused."""


def _under(name: str, package: str) -> bool:
    return name == package or name.startswith(f"{package}.")


def instrumentation_modules(modules: Iterable[str]) -> list[str]:
    """The names in ``modules`` that are a sign of tracing instrumentation."""
    return sorted(
        name
        for name in modules
        if _under(name, _INSTRUMENTATION_PACKAGE)
        or (
            any(_under(name, prefix) for prefix in _LOGFIRE_INTEGRATIONS)
            and name not in _LOGFIRE_BASELINE
        )
    )


def _pydantic_records() -> bool:
    """True when a pydantic plugin wraps validators (logfire's records).

    ``logfire.instrument_pydantic()``, or ``LOGFIRE_PYDANTIC_PLUGIN_RECORD``
    with no code at all, wraps every validator built after it, the
    TypeAdapters pydantic-ai builds included, and exports the validated
    values. No module name shows it, so two checks, either of which refuses:

    - logfire's plugin setting: anything but ``record`` off. Its include and
      exclude filters can skip this module's validators while recording
      pydantic-ai's, so the setting decides, not one validator. The plugin
      module is read from ``sys.modules`` (every process loads it through
      pydantic's plugin entry point), never imported here; if it is loaded
      but its setting can't be read, a logfire change, the check refuses.
    - a validator built here: pydantic-core's own methods are builtins, a
      plugin's wrappers are Python functions. That catches another plugin.
    """
    from pydantic import TypeAdapter

    plugin = sys.modules.get(_LOGFIRE_PYDANTIC_PLUGIN)
    if plugin is not None:
        try:
            record = plugin.get_pydantic_plugin_config().record
        except Exception:
            return True
        if record != "off":
            return True
    validator = TypeAdapter(dict[str, str]).validator
    return not all(
        isinstance(getattr(validator, method, None), BuiltinMethodType)
        for method in ("validate_python", "validate_json")
    )


def refuse_instrumentation(modules: Iterable[str] | None = None) -> None:
    """Refuse statement work if tracing instrumentation is loaded.

    Raises :class:`InstrumentationLoadedError` naming what it found (module
    names, never data). ``modules`` defaults to ``sys.modules``. Run when
    the statement agents are built, when ``serve`` starts, and at the start
    of every statement run, since an instrument call can come later in a
    long-lived process.

    Not everything is visible: pydantic-ai's ``Agent.instrument_all`` with a tracer
    provider loads no module, which every statement agent's own
    ``instrument = False`` covers. The tests and the deployment rule stay
    the guard (plan U5, Tracing).
    """
    found = instrumentation_modules(list(sys.modules if modules is None else modules))
    if _pydantic_records():
        found.append("a pydantic plugin recording validations")
    if found:
        msg = (
            "statement work is refused: tracing instrumentation is loaded "
            f"({', '.join(found)}), and it would export statement text"
        )
        raise InstrumentationLoadedError(msg)


@dataclass(frozen=True)
class StatementAgents:
    """The first-pass agent and one agent per account type."""

    first_pass: Agent[None, FirstPass]
    accounts: Mapping[AccountType, Agent[None, Any]]

    @classmethod
    def create(
        cls, model: Model, settings: ModelSettings | None = None
    ) -> StatementAgents:
        """Every statement agent, built only through the guarded constructors.

        ``model`` must be a model object from ``llm.anthropic_model`` (or a
        pydantic-ai test model): the constructors refuse a name or an
        Anthropic model whose client could trace. Refuses to build anything
        while tracing instrumentation is loaded.
        """
        refuse_instrumentation()
        return cls(
            first_pass=create_first_pass_agent(model, settings),
            accounts={
                account_type: create_account_agent(account_type, model, settings)
                for account_type in _ACCOUNT_TYPES
            },
        )


class StatementItemError(Exception):
    """An item error of the statement pipeline: retried, then terminal."""


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def add(self, usage: CallUsage | None) -> None:
        if usage is not None:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.requests += usage.requests


@dataclass(frozen=True)
class _Extracted:
    """A statement read and extracted, ready to write."""

    revision: RevisionRow
    gated: GatedStatement | None


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _step(control: RunControl | None, source_id: str) -> None:
    """Re-mark the item's watchdog clock: each phase is its own step."""
    if control is not None:
        control.step(source_id)


def masked_file_name(name: str | None) -> str | None:
    """A source item's file name with every long digit run cut (Masking)."""
    masked, _ = mask_digits(name)
    return masked


def _safe_message(exc: BaseException) -> str:
    """An error's text for ingest_log and the log, never model output.

    A statement call's error carries none by construction. A database error
    can quote a failing row's values in its detail, so only its type and
    SQLSTATE are kept; any other error type is kept by name alone unless it
    is one the pipeline raises itself.
    """
    if isinstance(
        exc, StatementCallError | StatementItemError | InstrumentationLoadedError
    ):
        return str(exc)
    if isinstance(exc, psycopg.Error):
        return f"{type(exc).__name__} (SQLSTATE {exc.sqlstate})"
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc.strerror or 'OS error'}"
    return type(exc).__name__


def _rejected(
    read: StatementRead | None,
    document_type: str,
    *,
    rejected_bound: str | None = None,
    model: str | None = None,
    usage: _Usage | None = None,
    printed_account_count: int | None = None,
) -> RevisionRow:
    return RevisionRow(
        origin="ingest",
        document_type=document_type,
        status="rejected",
        rejected_bound=rejected_bound,
        pages=None if read is None else read.pages,
        printed_account_count=printed_account_count,
        llm_model=model,
        prompt_version=None if model is None else str(STATEMENT_PROMPT_VERSION),
        first_pass_version=None if model is None else str(FIRST_PASS_VERSION),
        input_tokens=0 if usage is None else usage.input_tokens,
        output_tokens=0 if usage is None else usage.output_tokens,
    )


def _not_called(listed_type: AccountType) -> AccountRun:
    """An account whose pages lie past the document's: an empty extraction."""
    output: AccountExtraction | LoanExtraction = (
        LoanExtraction()
        if listed_type == "loan"
        else AccountExtraction(line_order="oldest_first")
    )
    return AccountRun(output=output, capped=False, usage=CallUsage())


def extract_statement(
    data: bytes,
    *,
    agents: StatementAgents,
    llm_model: str,
    source_id: str,
    control: RunControl | None,
    usage: _Usage,
) -> _Extracted:
    """Read and extract one statement: steps 3 to 5. No database access.

    Raises :class:`StatementItemError` for a read that should be retried,
    and lets a :class:`StatementCallError` through.
    """
    _step(control, source_id)
    try:
        read = read_statement(data, bounds=STATEMENT_BOUNDS)
    except OSError as exc:
        # Starting the reader failed (no processes or descriptors left, or a
        # full temporary directory): the same bounded, retried item error.
        msg = f"the statement reader couldn't start: {_safe_message(exc)}"
        raise StatementItemError(msg) from None
    logger.info(
        "Statement %s read: %s pages, encrypted=%s, reason=%s",
        source_id,
        read.pages,
        read.encrypted,
        read.reason,
    )
    if read.reason in _REJECTED_TYPES:
        return _Extracted(_rejected(read, _REJECTED_TYPES[read.reason]), None)
    if read.reason in _REJECTED_BOUNDS:
        return _Extracted(
            _rejected(read, "over_bounds", rejected_bound=read.reason), None
        )
    if read.reason is not None or read.page_texts is None or read.pages is None:
        # timeout (a CPU or wall-clock limit, or a kill) or failed.
        msg = f"the statement read stopped: {read.reason}"
        raise StatementItemError(msg)
    if len(read.page_texts) != read.pages:
        msg = f"the reader returned {len(read.page_texts)} pages of {read.pages}"
        raise StatementItemError(msg)
    pages = read.page_texts

    _step(control, source_id)
    first_run = run_first_pass(agents.first_pass, pages)
    usage.add(first_run.usage)
    first = first_run.output
    if first is None:  # capped: the account list doesn't fit
        return _Extracted(
            _rejected(
                read,
                "over_bounds",
                rejected_bound="accounts",
                model=llm_model,
                usage=usage,
            ),
            None,
        )
    if first.document_type not in STATEMENT_TYPES:
        return _Extracted(
            _rejected(read, first.document_type, model=llm_model, usage=usage),
            None,
        )
    if len(first.accounts) > MAX_ACCOUNTS:
        return _Extracted(
            _rejected(
                read,
                "over_bounds",
                rejected_bound="accounts",
                model=llm_model,
                usage=usage,
                printed_account_count=first.printed_account_count,
            ),
            None,
        )

    runs: list[AccountRun] = []
    for listed in first.accounts:
        if listed.last_page > read.pages:
            runs.append(_not_called(listed.account_type))
            continue
        _step(control, source_id)
        run = run_account(
            agents.accounts[listed.account_type], pages, listed, first.period
        )
        usage.add(run.usage)
        runs.append(run)

    try:
        gated = gate_statement(first, runs)
    except Exception as exc:
        # The gates raise one GateInputError with a fixed message; anything
        # else is a bug. Either way an item error, kept by its class alone,
        # so no message can quote the model's output.
        msg = f"the gates refused the statement: {type(exc).__name__}"
        raise StatementItemError(msg) from None
    period = period_dates(first.period)
    start, end = period if isinstance(period, tuple) else (None, None)
    revision = RevisionRow(
        origin="ingest",
        document_type=first.document_type,
        status=gated.status,
        hold_reason=gated.hold_reason,
        masked_runs=gated.masked_runs,
        period_start=start,
        period_end=end,
        printed_account_count=first.printed_account_count,
        pages=read.pages,
        llm_model=llm_model,
        prompt_version=str(STATEMENT_PROMPT_VERSION),
        first_pass_version=str(FIRST_PASS_VERSION),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
    return _Extracted(revision, gated)


def _money(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _account_row(account: GatedAccount) -> AccountRow:
    loan = account.loan
    return AccountRow(
        ordinal=account.ordinal,
        occurrence=account.occurrence,
        account_type=account.account_type,
        status=account.status,
        content_digest=bytes.fromhex(account.content_digest),
        institution=account.institution,
        last4=account.last4,
        currency=account.currency,
        period_start=account.period_start,
        period_end=account.period_end,
        held_by_statement=account.held_by_statement,
        failure_class=account.failure_class,
        difference=_money(account.difference),
        first_page=account.first_page,
        last_page=account.last_page,
        line_order=account.line_order,
        date_grace_days=account.date_grace_days,
        beginning_balance=_money(account.beginning_balance),
        ending_balance=_money(account.ending_balance),
        section_totals=[asdict(t) for t in account.section_totals],
        column_totals=[asdict(t) for t in account.column_totals],
        principal_before=None if loan is None else _money(loan.principal_before),
        principal_after=None if loan is None else _money(loan.principal_after),
        principal_paid=None if loan is None else _money(loan.principal_paid),
        interest=None if loan is None else _money(loan.interest),
        escrow=None if loan is None else _money(loan.escrow),
        fees=None if loan is None else _money(loan.fees),
        advances=None if loan is None else _money(loan.advances),
    )


def _line_rows(account: GatedAccount) -> list[LineRow]:
    return [
        LineRow(
            ordinal=line.ordinal,
            posted_date=line.posted_date,
            description=line.description,
            amount=Decimal(line.amount),
            page=line.page,
            transaction_date=line.transaction_date,
            running_balance=_money(line.running_balance),
            section=line.section,
        )
        for line in account.lines
    ]


def _write_accounts(
    conn: psycopg.Connection[dict[str, Any]],
    revision_id: UUID,
    accounts: Sequence[GatedAccount],
) -> None:
    for account in accounts:
        account_id = insert_account(conn, revision_id, _account_row(account))
        if account.lines:
            insert_lines(conn, account_id, _line_rows(account))


def _success_row(
    conn: psycopg.Connection[dict[str, Any]],
    raw: RawReceipt,
    *,
    source_name: str,
    llm_model: str | None,
    usage: _Usage,
    state: AttemptState,
) -> None:
    insert_ingest_log(
        conn,
        source_id=raw.source_id,
        source_type=STATEMENT_SOURCE_TYPE,
        source_name=source_name,
        status="success",
        email_subject=None,
        email_sender=None,
        email_date=None,
        llm_input_tokens=usage.input_tokens,
        llm_output_tokens=usage.output_tokens,
        llm_requests=usage.requests,
        llm_model=llm_model if usage.requests else None,
        attempt=state.number,
        commit=False,
    )


def _record_failure(
    conn: psycopg.Connection[dict[str, Any]],
    raw: RawReceipt,
    *,
    source_name: str,
    exc: BaseException,
    error_class: ErrorClass,
    usage: _Usage,
    llm_model: str,
    policy: RetryPolicy,
    state: AttemptState,
) -> None:
    fields = failure_fields(policy, state, error_class, _now())
    try:
        insert_ingest_log(
            conn,
            source_id=raw.source_id,
            source_type=STATEMENT_SOURCE_TYPE,
            source_name=source_name,
            status="failed",
            email_subject=None,
            email_sender=None,
            email_date=None,
            error_message=_safe_message(exc),
            llm_input_tokens=usage.input_tokens,
            llm_output_tokens=usage.output_tokens,
            llm_requests=usage.requests,
            llm_model=llm_model if usage.requests else None,
            attempt=fields.attempt,
            counts_attempt=fields.counts_attempt,
            next_retry_at=fields.next_retry_at,
            error_class=fields.error_class,
        )
    except Exception as write_error:
        logger.warning(
            "Failed to write ingest log for %s: %s",
            raw.source_id,
            _safe_message(write_error),
        )


def _error_class(exc: BaseException) -> ErrorClass:
    """How a statement failure is retried.

    A statement call's class was taken from its original error. A request
    timeout counts as an attempt (an item error), so a slow account is
    retried a bounded number of times, never forever. The pipeline's own
    item errors are item errors.
    """
    if isinstance(exc, StatementCallError):
        if exc.kind == "timeout":
            return "content"
        return exc.error_class
    if isinstance(exc, StatementItemError):
        return "content"
    if isinstance(exc, InstrumentationLoadedError):
        # The process, not the item: stop the source, count no attempt.
        return "config"
    return classify(exc)


def process_statement(
    conn: psycopg.Connection[dict[str, Any]],
    raw: RawReceipt,
    *,
    store: EvidenceStore,
    source_name: str,
    agents: StatementAgents,
    llm_model: str,
    policy: RetryPolicy | None = None,
    state: AttemptState | None = None,
    control: RunControl | None = None,
) -> ItemOutcome:
    """Run one statement item, already logged as ``started``.

    A held or rejected statement is a success: its revision is written. Any
    error writes no revision and records a ``failed`` row: a transient or
    config one stops the source (``ItemOutcome.stops_source``), an item
    error is retried with the usual backoff.
    """
    policy = policy or RetryPolicy()
    state = state or AttemptState(1)
    usage = _Usage()
    data = raw.raw_bytes or raw.file_content
    try:
        # At the start of every run: an instrument call can come after
        # serve started. A refusal stops the source as a config failure.
        refuse_instrumentation()
        if not data:
            msg = "the statement item has no file content"
            raise StatementItemError(msg)
        digest = hashlib.sha256(data).digest()
        file_name = masked_file_name(raw.file_name)
        received_at = raw.date or _now()

        existing = statement_for_sha256(conn, digest)
        if existing is not None:
            add_source(
                conn,
                statement_id=existing,
                source_name=source_name,
                source_id=raw.source_id,
                file_name=file_name,
                received_at=received_at,
            )
            _success_row(
                conn,
                raw,
                source_name=source_name,
                llm_model=None,
                usage=usage,
                state=state,
            )
            conn.commit()
            logger.info(
                "Statement %s is a copy of statement %s", raw.source_id, existing
            )
            return ItemOutcome("success")
        conn.commit()

        stored = put_original(
            conn,
            store,
            data,
            media_type=_PDF,
            folder=StoreFolder.for_original(received_at, source_name, digest),
            name=file_name or "statement.pdf",
        )
        # End the read transaction before the PDF read and the model calls.
        conn.commit()
        extracted = extract_statement(
            data,
            agents=agents,
            llm_model=llm_model,
            source_id=raw.source_id,
            control=control,
            usage=usage,
        )

        # One short transaction, in the U2 insert order.
        original_id = record_original(
            conn,
            ref=stored.ref,
            sha256=digest,
            byte_size=len(data),
            media_type=_PDF,
        )
        statement_id = new_statement_id(conn)
        revision_id, _ = insert_revision(conn, statement_id, extracted.revision)
        if extracted.gated is not None:
            _write_accounts(conn, revision_id, extracted.gated.accounts)
        create_statement(
            conn,
            statement_id=statement_id,
            original_file_id=original_id,
            source_sha256=digest,
            revision_id=revision_id,
            status=extracted.revision.status,
            document_type=extracted.revision.document_type,
        )
        add_source(
            conn,
            statement_id=statement_id,
            source_name=source_name,
            source_id=raw.source_id,
            file_name=file_name,
            received_at=received_at,
        )
        _success_row(
            conn,
            raw,
            source_name=source_name,
            llm_model=llm_model,
            usage=usage,
            state=state,
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if isinstance(exc, StatementCallError):
            usage.add(exc.usage)
        cls = _error_class(exc)
        _record_failure(
            conn,
            raw,
            source_name=source_name,
            exc=exc,
            error_class=cls,
            usage=usage,
            llm_model=llm_model,
            policy=policy,
            state=state,
        )
        # By type and message only: never a traceback, which could hold
        # statement text in its frames or a database row in its detail.
        logger.warning(
            "Statement %s failed (%s): %s", raw.source_id, cls, _safe_message(exc)
        )
        return ItemOutcome("failed", error_class=cls)
    logger.info(
        "Statement %s: %s, %s",
        raw.source_id,
        extracted.revision.status,
        extracted.revision.document_type,
    )
    return ItemOutcome("success")
