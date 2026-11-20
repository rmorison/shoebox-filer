"""CLI entry point for receipt-index."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast
from uuid import UUID

import click
from dotenv import load_dotenv
from psycopg.errors import DeadlockDetected

from receipt_index.config import AppConfig, ConfigError, load_config
from receipt_index.duplicates import DEFAULT_WINDOW_DAYS
from receipt_index.models import KINDS
from receipt_index.paging import page_envelope

# Load .env before any config interpolation
load_dotenv()

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from datetime import datetime
    from decimal import Decimal

    from receipt_index.adapters.base import SourceAdapter
    from receipt_index.config import SourceConfig
    from receipt_index.duplicates import Actor, CandidateStatus
    from receipt_index.models import IngestLogEntry, Receipt, ReceiptEvent
    from receipt_index.requeue import OrphanReport, RequeueOutcome
    from receipt_index.statements import StatementAgents

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Third-party loggers that emit per-page/per-glyph records at DEBUG/INFO and
# drown out application logs (notably in container output).
_NOISY_LIBRARY_LOGGERS = ("pdfminer", "fontTools", "PIL")


def _configure_logging(config: AppConfig) -> None:
    """Configure application logging and quiet noisy third-party libraries.

    The noisy library loggers are always clamped to WARNING, even when the
    application level is DEBUG. Debugging those libraries requires raising
    their level explicitly.

    Args:
        config: Loaded application configuration.
    """
    # force=True: reconfigure deterministically even if a handler was already
    # installed on the root logger (e.g. by an embedding process or test runner).
    logging.basicConfig(
        level=getattr(logging, config.logging.level, logging.INFO),
        format=_LOG_FORMAT,
        force=True,
    )
    for name in _NOISY_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # And never let a library log a model request or response.
    from receipt_index.llm import quiet_payload_loggers

    quiet_payload_loggers()


def _load_config_or_exit(ctx: click.Context) -> AppConfig:
    """Load AppConfig from Click context, exiting with a clear message on error."""
    config_path: str | None = ctx.obj.get("config_path") if ctx.obj else None
    try:
        return load_config(config_path)
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=False),
    default=None,
    envvar="RECEIPT_INDEX_CONFIG",
    help="Path to YAML config file.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """Receipt Search Index -- find receipts fast."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command()
@click.option(
    "--source",
    "source_name",
    default=None,
    help="Ingest from a specific named source only.",
)
@click.option("--dry-run", is_flag=True, help="Preview without processing.")
@click.option("--limit", type=int, default=None, help="Max messages to process.")
@click.pass_context
def ingest(
    ctx: click.Context,
    source_name: str | None,
    dry_run: bool,
    limit: int | None,
) -> None:
    """Ingest receipts from configured sources.

    Each source is read from where the last run stopped, then its items that
    are due for a retry are run (failed earlier, or requeued with `retry`).
    A source that a running process holds is skipped; the exit code is then 3.
    """
    from contextlib import ExitStack

    from receipt_index.adapters.imap import ImapAdapter
    from receipt_index.config import GdriveSourceConfig, ImapSourceConfig
    from receipt_index.db import INGEST_STATEMENT_TIMEOUT_SECONDS, get_connection
    from receipt_index.fetching import SourcePool, ingest_type
    from receipt_index.locks import SourceLock
    from receipt_index.pipeline import IngestResult, run_due_retries, run_ingest
    from receipt_index.repository import select_due
    from receipt_index.retries import RetryPolicy
    from receipt_index.store import open_store

    config = _load_config_or_exit(ctx)

    _configure_logging(config)

    # Filter sources if --source is specified
    sources = config.sources
    if source_name is not None:
        sources = [s for s in sources if s.name == source_name]
        if not sources:
            available = ", ".join(s.name for s in config.sources)
            click.echo(
                f"Error: source {source_name!r} not found in config. "
                f"Available sources: {available}",
                err=True,
            )
            sys.exit(1)

    store = open_store(config.store)

    agents = _extraction_agents(config, config.llm.model)
    email_agent, document_agent = agents["email"], agents["document"]
    # Statement sources go to the statement pipeline, as in `serve`.
    statement_agents = None if dry_run else _statement_agents(config, sources)
    policy = RetryPolicy.from_config(config.ingest)
    totals = IngestResult()
    source_results: list[tuple[str, IngestResult]] = []
    locked_sources: list[str] = []
    failed_sources: list[str] = []
    # The retry adapters are closed when the command's context ends.
    stack = ctx.with_resource(ExitStack())
    pool = SourcePool(config.sources, _source_adapter(config), stack)
    counted_due: set[str] = set()

    for source_config in sources:
        if isinstance(source_config, ImapSourceConfig):
            adapter: SourceAdapter = ImapAdapter(source_config)
            agent = email_agent
        elif isinstance(source_config, GdriveSourceConfig):
            from receipt_index.adapters.gdrive import GdriveAdapter

            adapter = GdriveAdapter(
                source_config,
                settle_seconds=config.ingest.drive_settle_seconds,
            )
            agent = document_agent
        else:
            logger.warning("Unknown source type, skipping %r", source_config.name)
            continue

        with SourceLock(config.database.url, source_config.name) as source_lock:
            if not source_lock.acquire():
                click.echo(
                    f"source {source_config.name} is being watched by a running "
                    "serve; see `receipt-index status`",
                    err=True,
                )
                locked_sources.append(source_config.name)
                continue

            with get_connection(
                config.database.url,
                statement_timeout_seconds=INGEST_STATEMENT_TIMEOUT_SECONDS,
            ) as conn:
                result = run_ingest(
                    conn=conn,
                    adapter=adapter,
                    store=store,
                    source_name=source_config.name,
                    source_type=source_config.type,
                    item_type=ingest_type(source_config),
                    agent=agent,
                    llm_model=config.llm.model,
                    dry_run=dry_run,
                    limit=limit,
                    source_lock=source_lock,
                    policy=policy,
                    statement_agents=statement_agents,
                )
                # Retries for the sources this run holds, after the read; not
                # when the read itself stopped.
                if result.source_error is None:
                    due = select_due(
                        conn,
                        source_name=source_config.name,
                        source_type=ingest_type(source_config),
                        configured=[s.name for s in config.sources],
                        limit=limit,
                    )
                    if dry_run:
                        # An item that names no configured source is due in
                        # every pass of its type: count it once.
                        due = [d for d in due if d.source_id not in counted_due]
                        counted_due.update(d.source_id for d in due)
                        if due:
                            click.echo(
                                f"Source {source_config.name}: "
                                f"{len(due)} item(s) due for a retry"
                            )
                    elif due:
                        retried = run_due_retries(
                            conn,
                            due,
                            pool=pool,
                            store=store,
                            agents=agents,
                            llm_model=config.llm.model,
                            policy=policy,
                            own_source=source_config.name,
                            statement_agents=statement_agents,
                        )
                        result.processed += retried.processed
                        result.skipped += retried.skipped
                        result.failed += retried.failed
                        result.receipts.extend(retried.receipts)
                        if retried.stopped is not None:
                            result.source_error = f"retries {retried.stopped}"

        if result.source_error is not None:
            click.echo(
                f"Error: source {source_config.name}: {result.source_error}",
                err=True,
            )
            failed_sources.append(source_config.name)
        source_results.append((source_config.name, result))
        totals.processed += result.processed
        totals.skipped += result.skipped
        totals.failed += result.failed

    # Report per-source summaries when multiple sources
    if len(source_results) > 1:
        for name, result in source_results:
            click.echo(
                f"Source {name}: "
                f"Processed: {result.processed}  "
                f"Skipped: {result.skipped}  "
                f"Failed: {result.failed}"
            )
        click.echo(
            f"Total: Processed: {totals.processed}  "
            f"Skipped: {totals.skipped}  "
            f"Failed: {totals.failed}"
        )
    else:
        click.echo(
            f"Processed: {totals.processed}  "
            f"Skipped: {totals.skipped}  "
            f"Failed: {totals.failed}"
        )

    # 3 if any source was held by another process, otherwise 1 if any item
    # or source failed.
    if locked_sources:
        sys.exit(3)
    if totals.failed > 0 or failed_sources:
        sys.exit(1)


@cli.command()
@click.option(
    "--source",
    "source_names",
    multiple=True,
    help="Watch only this source (repeatable). Default: every source.",
)
@click.pass_context
def serve(ctx: click.Context, source_names: tuple[str, ...]) -> None:
    """Watch the sources and ingest new items as they arrive, until stopped.

    IMAP sources are watched with IDLE (plus a safety poll), Drive sources
    are polled, and items due for a retry are run every minute. SIGTERM or
    Ctrl-C stops it after the item in hand. A source that another process
    holds is left out; if every source is held, the exit code is 3.
    """
    import signal

    from receipt_index.db import (
        INGEST_CONNECT_TIMEOUT_SECONDS,
        INGEST_STATEMENT_TIMEOUT_SECONDS,
        get_connection,
    )
    from receipt_index.health import HEARTBEAT_PATH
    from receipt_index.locks import SourceLock
    from receipt_index.retries import RetryPolicy
    from receipt_index.service import ServeTimings, Service
    from receipt_index.store import open_store

    config = _load_config_or_exit(ctx)
    _configure_logging(config)

    sources = list(config.sources)
    if source_names:
        known = {s.name for s in config.sources}
        unknown = [n for n in source_names if n not in known]
        if unknown:
            available = ", ".join(sorted(known))
            click.echo(
                f"Error: source(s) not found in config: {', '.join(unknown)}. "
                f"Available sources: {available}",
                err=True,
            )
            sys.exit(1)
        sources = [s for s in sources if s.name in source_names]

    locks: dict[str, SourceLock] = {}
    finished = True
    held: list[SourceConfig] = []
    try:
        for source_config in sources:
            lock = SourceLock(config.database.url, source_config.name)
            if lock.acquire():
                locks[source_config.name] = lock
                held.append(source_config)
            else:
                click.echo(
                    f"source {source_config.name} is being watched by a running "
                    "serve; see `receipt-index status`",
                    err=True,
                )
        if not held:
            sys.exit(3)

        agents = _extraction_agents(config, config.llm.model)
        ingest_config = config.ingest
        health = ingest_config.health
        # Only the sources this serve holds: their retry passes are the only
        # ones that take statement items.
        statement_agents = _statement_agents(config, held)
        service = Service(
            sources=held,
            all_sources=config.sources,
            locks=locks,
            timings=ServeTimings(
                idle_reissue_seconds=ingest_config.idle_reissue_seconds,
                imap_poll_seconds=ingest_config.imap_poll_seconds,
                drive_poll_seconds=ingest_config.drive_poll_seconds,
                item_timeout_seconds=ingest_config.item_timeout_seconds,
            ),
            open_conn=lambda: get_connection(
                config.database.url,
                statement_timeout_seconds=INGEST_STATEMENT_TIMEOUT_SECONDS,
                connect_timeout_seconds=INGEST_CONNECT_TIMEOUT_SECONDS,
            ),
            adapter_factory=_source_adapter(config),
            agents=agents,
            store=open_store(config.store),
            llm_model=config.llm.model,
            policy=RetryPolicy.from_config(ingest_config),
            heartbeat_path=HEARTBEAT_PATH,
            ping_url=(health.ping_url.get_secret_value() if health.ping_url else None),
            ping_interval_seconds=health.ping_interval_seconds,
            ping_probe=_ping_probe(config),
            requested=[s.name for s in sources],
            statement_agents=statement_agents,
        )

        def _on_signal(signum: int, _frame: object) -> None:
            # Only a flag: logging or Event.set here could deadlock on a lock
            # the interrupted main thread holds.
            service.request_stop()

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
        logger.info("serve: watching %s", ", ".join(s.name for s in held))
        service.start()
        service.wait()
        finished = service.shutdown()
        logger.info("serve: stopped")
    finally:
        # A worker still running may be using its lock connection: leave the
        # locks to process exit, which drops them with their connections.
        if finished:
            for lock in locks.values():
                lock.release()
    if not finished:
        sys.exit(1)


def _status_connection(config: AppConfig) -> Callable[[], Any]:
    """A short-leashed read-only connection for `status` and the ping."""
    from receipt_index.db import get_connection
    from receipt_index.health import (
        STATUS_CONNECT_TIMEOUT_SECONDS,
        STATUS_STATEMENT_TIMEOUT_SECONDS,
    )

    return lambda: get_connection(
        config.database.url,
        read_only=True,
        statement_timeout_seconds=STATUS_STATEMENT_TIMEOUT_SECONDS,
        connect_timeout_seconds=STATUS_CONNECT_TIMEOUT_SECONDS,
    )


def _ping_probe(config: AppConfig) -> Callable[[], list[str]]:
    """One ping round's checks: the same as `status --check`, plus terminals."""
    from receipt_index.health import HEARTBEAT_PATH, collect, ping_lines

    open_conn = _status_connection(config)

    def probe() -> list[str]:
        report = collect(
            config, open_conn, heartbeat_path=HEARTBEAT_PATH, retries=False
        )
        return ping_lines(report, config.ingest.health.terminal_alert_seconds)

    return probe


@cli.command()
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Exit 1, printing one line per problem, unless everything is healthy.",
)
@click.pass_context
def status(ctx: click.Context, output_format: str, check: bool) -> None:
    """Show each source's sync state, serve's heartbeat, the retry counts, and
    the statement reads the MCP server withheld.

    Works with or without `serve` running: it reads the database, and the
    heartbeat file if there is one. With --check it prints only the problem
    lines (or "ok") and exits 1 if there is any; it is serve's container
    healthcheck. Terminal failures never fail the check.
    """
    from receipt_index.access_log import configured_access_log_path
    from receipt_index.health import HEARTBEAT_PATH, collect, report_json, report_text

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    report = collect(
        config,
        _status_connection(config),
        heartbeat_path=HEARTBEAT_PATH,
        # The healthcheck needs no retry counts: fewer queries under its
        # timeout.
        retries=not (check and output_format == "text"),
        # Statement reads the MCP server withheld (its access log), so the
        # operator sees them without reading the log.
        access_log_path=configured_access_log_path(),
    )

    if output_format == "json":
        click.echo(json.dumps(report_json(report), indent=2))
    elif check:
        for problem in report.problems:
            click.echo(problem.line)
        if not report.problems:
            click.echo("ok")
    else:
        for line in report_text(report):
            click.echo(line)

    if check and report.problems:
        sys.exit(1)
    if not report.database_ok:
        sys.exit(1)


def _text_filter(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> str | None:
    """A text filter, trimmed; blank is refused rather than matching all."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise click.BadParameter("must not be blank")
    return value


_FOUR_DIGITS = re.compile(r"[0-9]{4}")


def _last4_filter(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> str | None:
    """Exactly four digits: the only part of a card number that is stored."""
    if value is None:
        return None
    value = value.strip()
    if not _FOUR_DIGITS.fullmatch(value):
        raise click.BadParameter("must be exactly four digits")
    return value


@cli.command()
@click.option("--source", "source_name", default=None, help="Filter by source name.")
@click.option("--vendor", default=None, help="Filter by vendor name (substring).")
@click.option(
    "--amount",
    default=None,
    help="Filter by exact effective amount (paid when known, else the total).",
)
@click.option("--amount-min", default=None, help="Filter by minimum effective amount.")
@click.option("--amount-max", default=None, help="Filter by maximum effective amount.")
@click.option(
    "--date-from",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Filter by start date (YYYY-MM-DD).",
)
@click.option(
    "--date-to",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Filter by end date (YYYY-MM-DD).",
)
@click.option(
    "--needs-review",
    is_flag=True,
    help=(
        "Only receipts still missing a paid amount, or whose latest extraction "
        "failed or was below the confidence gate; lowest paid-amount "
        "confidence first. Link-only receipts aren't indexed: the text output "
        "counts them, and `failures --status link-only` lists them."
    ),
)
@click.option(
    "--order-number",
    callback=_text_filter,
    default=None,
    help="Filter by order number (exact).",
)
@click.option(
    "--last4",
    callback=_last4_filter,
    default=None,
    help="Filter by the payment card's or account's last four digits.",
)
@click.option(
    "--payer",
    callback=_text_filter,
    default=None,
    help="Filter by payer name (substring, any case).",
)
@click.option(
    "--kind",
    "kinds",
    multiple=True,
    type=click.Choice(KINDS),
    help=(
        "Only this kind (repeatable): purchase, refund, credit or statement. "
        "Default: every kind."
    ),
)
@click.option(
    "--include-merged",
    is_flag=True,
    help=(
        "Also list receipts merged into another as duplicates. Leave it off "
        "for totals, so a duplicate counts once."
    ),
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Print at most N receipts. Default: every match.",
)
@click.option(
    "--offset",
    type=click.IntRange(min=0),
    default=None,
    help="Skip the first N matches. Default: 0.",
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def search(
    ctx: click.Context,
    source_name: str | None,
    vendor: str | None,
    amount: str | None,
    amount_min: str | None,
    amount_max: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    needs_review: bool,
    order_number: str | None,
    last4: str | None,
    payer: str | None,
    kinds: tuple[str, ...],
    include_merged: bool,
    limit: int | None,
    offset: int | None,
    output_format: str,
) -> None:
    """Search indexed receipts.

    A receipt merged into another as a duplicate is left out unless
    --include-merged. JSON output lists each receipt's provenance (its
    source items) and suspected_duplicate_of (open pairs to review).

    JSON output gives each receipt's kind and signed_amount: a sum of
    signed_amount is the net spend (a refund or credit counts against it, a
    statement not at all). A purchase also gives refunded and net_paid.

    --limit and --offset page the same order the MCP search_receipts tool
    does. JSON output is the same page envelope as that tool's: {receipts,
    total, limit, offset, next_offset}, with limit and next_offset null
    without --limit.
    """
    from decimal import Decimal, InvalidOperation

    import psycopg

    from receipt_index.db import get_connection
    from receipt_index.repository import (
        count_ingest_failures,
        count_receipts,
        search_receipts,
        with_provenance,
    )

    config = _load_config_or_exit(ctx)

    _configure_logging(config)

    if amount is not None and (amount_min is not None or amount_max is not None):
        raise click.UsageError(
            "--amount cannot be combined with --amount-min/--amount-max"
        )

    def _to_decimal(value: str | None, name: str) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(value)
        except InvalidOperation:
            msg = f"Invalid decimal value for {name}: {value}"
            raise click.BadParameter(msg) from None

    amount_d = _to_decimal(amount, "--amount")
    amount_min_d = _to_decimal(amount_min, "--amount-min")
    amount_max_d = _to_decimal(amount_max, "--amount-max")

    filters: dict[str, Any] = {
        "vendor": vendor,
        "amount": amount_d,
        "amount_min": amount_min_d,
        "amount_max": amount_max_d,
        "date_from": date_from.date() if date_from else None,
        "date_to": date_to.date() if date_to else None,
        "source_name": source_name,
        "needs_review": needs_review,
        "order_number": order_number,
        "last4": last4,
        "payer": payer,
        "kinds": list(kinds) or None,
        "include_merged": include_merged,
    }
    paged = limit is not None or offset is not None
    total: int | None = None

    with get_connection(config.database.url) as conn:
        if paged or output_format == "json":
            # The JSON envelope and the paged text footer place the page among
            # all matches: count them in one read-only snapshot, as the MCP
            # tool does, so the total and the page agree.
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            total = count_receipts(conn, **filters)
        results = with_provenance(
            conn, search_receipts(conn, **filters, limit=limit, offset=offset)
        )
        # Items with no receipt to search: counted for the text output only,
        # so the JSON matches the MCP envelope.
        link_only = (
            count_ingest_failures(conn, status="link-only", source_name=source_name)
            if needs_review and output_format == "text"
            else 0
        )

    if output_format == "json":
        data = page_envelope(
            "receipts",
            [r.model_dump(mode="json") for r in results],
            total=cast("int", total),
            limit=limit,
            offset=offset or 0,
        )
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        _print_receipts(results, total=total, offset=offset or 0)
        if link_only:
            click.echo(
                f"\n{link_only} link-only item(s) (not filtered by vendor, "
                "amount or date): receipts that are only a link aren't indexed. See "
                "`receipt-index failures --status link-only`."
            )


def _clip(text: str, width: int) -> str:
    """``text`` cut to ``width`` characters, with an ellipsis when cut."""
    return text[: width - 1] + "\u2026" if len(text) > width else text


def _payment_line(receipt: Receipt) -> str | None:
    """``card ending 1234 (payer)``, from whichever parts are known."""
    parts = []
    if receipt.payment_method:
        parts.append(receipt.payment_method)
    if receipt.payment_last4:
        parts.append(f"ending {receipt.payment_last4}")
    if receipt.payer:
        parts.append(f"({receipt.payer})" if parts else receipt.payer)
    return " ".join(parts) or None


def _duplicate_note(r: Receipt) -> str:
    """'merged' for a receipt merged away (it doesn't count in a total), 'dup?'
    for one with an open suspected-duplicate pair, else nothing."""
    if r.merged_into_id is not None:
        return "merged"
    if r.suspected_duplicate_of:
        return "dup?"
    return ""


def _print_receipts(
    results: list[Receipt], *, total: int | None = None, offset: int = 0
) -> None:
    """The `search` text table.

    ``total`` is the unpaged match count, given only when the results are a
    page (``--limit`` or ``--offset``); the footer then places the page in it.
    """
    if not results:
        if total:
            click.echo(f"No receipts on this page ({total} match).")
        else:
            click.echo("No receipts found.")
        return
    # Amount is the effective amount (paid when known, else the total),
    # unsigned; Kind says which way it counts.
    click.echo(
        f"{'ID':<38} {'Date':<12} {'Vendor':<20} {'Order':<16} {'Last4':<5} "
        f"{'Kind':<9} {'Amount':>10} {'Total':>10}  Note"
    )
    click.echo("-" * 134)
    for r in results:
        click.echo(
            f"{r.id!s:<38} "
            f"{r.receipt_date.isoformat():<12} "
            f"{_clip(r.vendor, 20):<20} "
            f"{_clip(r.order_number or '', 16):<16} "
            f"{r.payment_last4 or '':<5} "
            f"{r.kind:<9} "
            f"{r.effective_amount!s:>10} "
            f"{r.amount!s:>10}  "
            f"{_duplicate_note(r)}".rstrip()
        )
    if total is None:
        click.echo(f"\n{len(results)} receipt(s) found.")
    else:
        click.echo(
            f"\n{len(results)} receipt(s) shown "
            f"({offset + 1}-{offset + len(results)} of {total})."
        )


def _print_orphans(config: AppConfig, output_format: str) -> None:
    """`failures --orphans`: renditions left by failed attempts."""
    from receipt_index.db import get_connection
    from receipt_index.repository import orphaned_refs
    from receipt_index.requeue import stored_file_exists
    from receipt_index.store import open_store

    store = open_store(config.store)
    with get_connection(config.database.url) as conn:
        found = orphaned_refs(conn, None)
    rows = [
        {"source_id": sid, "stored_ref": ref, "exists": stored_file_exists(store, ref)}
        for sid, refs in found.items()
        for ref in refs
    ]
    if output_format == "json":
        click.echo(json.dumps(rows, indent=2))
        return
    present = [r for r in rows if r["exists"]]
    if not present:
        click.echo("No orphaned files.")
        return
    for row in present:
        click.echo(f"{row['source_id']}  {row['stored_ref']}")
    click.echo(
        f"\n{len(present)} orphaned file(s): stored by a failed attempt, "
        "referenced by no receipt. Nothing was deleted."
    )


def _aware(value: datetime | None) -> datetime | None:
    """A ``--since`` date, read as local midnight."""
    return value.astimezone() if value is not None else None


@cli.command()
@click.option(
    "--status",
    type=click.Choice(
        ["failed", "retrying", "terminal", "skipped", "queued", "link-only"]
    ),
    default="failed",
    show_default=True,
    help=(
        "Items whose latest row is: failed (retrying or terminal); retrying "
        "(due again later); terminal (not retried automatically); skipped "
        "(judged not a receipt); queued (requeued by `retry`); link-only "
        "(skipped because the receipt is only a link, which ingest doesn't "
        "follow; --output json gives the link)."
    ),
)
@click.option(
    "--since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Only items whose latest row is from this date on (YYYY-MM-DD).",
)
@click.option("--source", "source_name", default=None, help="Filter by source name.")
@click.option(
    "--orphans",
    is_flag=True,
    help=(
        "Instead, list stored PDFs that a failed attempt left behind and no "
        "receipt references (whatever the item's state now)."
    ),
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Print at most N items. Default: every match.",
)
@click.option(
    "--offset",
    type=click.IntRange(min=0),
    default=None,
    help="Skip the first N matches. Default: 0.",
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def failures(
    ctx: click.Context,
    status: str,
    since: datetime | None,
    source_name: str | None,
    orphans: bool,
    limit: int | None,
    offset: int | None,
    output_format: str,
) -> None:
    """List items whose latest ingest attempt failed (or was skipped).

    One line per item, newest first. --limit and --offset page that order.
    JSON output is the same page envelope as the MCP list_failures tool's:
    {failures, total, limit, offset, next_offset}. With --orphans, list the
    PDFs that failed attempts left behind instead; nothing is deleted.
    """
    import psycopg

    from receipt_index.db import get_connection
    from receipt_index.models import link_host
    from receipt_index.repository import count_ingest_failures, get_ingest_failures

    config = _load_config_or_exit(ctx)

    _configure_logging(config)

    if orphans:
        if (
            since is not None
            or source_name is not None
            or status != "failed"
            or limit is not None
            or offset is not None
        ):
            raise click.UsageError(
                "--orphans lists every orphaned file; it takes no --status, "
                "--since, --source, --limit or --offset"
            )
        _print_orphans(config, output_format)
        return

    filters: dict[str, Any] = {
        "status": status,
        "since": _aware(since),
        "source_name": source_name,
    }
    paged = limit is not None or offset is not None
    total: int | None = None

    with get_connection(config.database.url) as conn:
        if paged or output_format == "json":
            # The total and the page from one read-only snapshot, as in
            # `search` and the MCP tool.
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            total = count_ingest_failures(conn, **filters)
        results = get_ingest_failures(conn, **filters, limit=limit, offset=offset)

    if output_format == "json":
        data = page_envelope(
            "failures",
            [r.model_dump(mode="json") for r in results],
            total=cast("int", total),
            limit=limit,
            offset=offset or 0,
        )
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        if not results:
            if total:
                click.echo(f"None on this page ({total} match).")
            else:
                click.echo(
                    "No failed ingests." if status == "failed" else "None found."
                )
            return
        click.echo(
            f"{'Date':<22} {'Sender':<30} {'Subject':<40} {'Error':<30} "
            f"{'Class':<10} {'Next retry':<16}"
        )
        click.echo("-" * 152)
        for r in results:
            email_date = (
                r.email_date.strftime("%Y-%m-%d %H:%M") if r.email_date else "\u2014"
            )
            sender = (r.email_sender or "\u2014")[:29]
            subject = (r.email_subject or r.source_id or "\u2014")[:39]
            if r.receipt_link is not None:
                # The host alone: the path and query can carry a token.
                error = f"link: {link_host(r.receipt_link)}"[:29]
            else:
                error = (r.error_message or "\u2014")[:29]
            klass = r.error_class or "\u2014"
            if r.status == "failed" and r.next_retry_at is None:
                next_retry = "terminal"
            elif r.next_retry_at is not None:
                next_retry = r.next_retry_at.astimezone().strftime("%Y-%m-%d %H:%M")
            else:
                next_retry = "\u2014"
            click.echo(
                f"{email_date:<22} {sender:<30} {subject:<40} {error:<30} "
                f"{klass:<10} {next_retry:<16}"
            )
        noun = "failed ingest(s)" if status == "failed" else f"{status} item(s)"
        if total is None:
            click.echo(f"\n{len(results)} {noun}.")
        else:
            start = (offset or 0) + 1
            click.echo(
                f"\n{len(results)} {noun} shown "
                f"({start}-{start + len(results) - 1} of {total})."
            )


def _print_retry(
    candidates: list[IngestLogEntry],
    report: OrphanReport,
    outcome: RequeueOutcome | None,
    *,
    dry_run: bool,
    fmt: str,
) -> None:
    """Print what `retry` matched, and what it did."""
    if fmt == "json":
        data: dict[str, Any] = {
            "dry_run": dry_run,
            "matched": len(candidates),
            "items": [
                {
                    "source_id": c.source_id,
                    "source_name": c.source_name,
                    "status": c.status,
                    "error_class": c.error_class,
                    "attempt": c.attempt,
                    "logged_at": c.created_at.isoformat(),
                    "subject": c.email_subject,
                    "sender": c.email_sender,
                    "error": c.error_message,
                }
                for c in candidates
            ],
            "orphaned_files": report.files,
            "orphans_not_recorded": report.unrecorded,
        }
        if outcome is not None:
            data["request_id"] = str(outcome.request_id)
            data["requeued"] = outcome.requeued
            data["not_requeued"] = [
                {"source_id": sid, "reason": why} for sid, why in outcome.not_requeued
            ]
        click.echo(json.dumps(data, indent=2, default=str))
        return

    if not candidates:
        click.echo("No items match.")
        return
    click.echo(
        f"{'Logged':<17} {'Status':<8} {'Class':<10} {'Att':>3} "
        f"{'Subject or source ID':<40} {'Error':<40}"
    )
    click.echo("-" * 123)
    for c in candidates:
        logged = c.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
        subject = (c.email_subject or c.source_id)[:39]
        error = (c.error_message or "\u2014")[:39]
        klass = c.error_class or "\u2014"
        click.echo(
            f"{logged:<17} {c.status:<8} {klass:<10} "
            f"{c.attempt:>3} {subject:<40} {error:<40}"
        )
    for source_id, refs in report.files.items():
        for ref in refs:
            click.echo(f"Orphaned file for {source_id}: {ref}")
    if report.unrecorded:
        click.echo(
            f"{len(report.unrecorded)} item(s) failed before stored files were "
            "recorded: check the renditions folder by hand for orphans."
        )
    if outcome is None:
        click.echo(
            f"\nDry run: {len(candidates)} item(s) would be requeued. "
            "Pass --yes to requeue them."
        )
        return
    for source_id, why in outcome.not_requeued:
        click.echo(f"Not requeued: {source_id}: {why}")
    click.echo(
        f"\nRequeued {len(outcome.requeued)} of {len(candidates)} item(s) "
        f"(request {outcome.request_id}). The next ingest runs them."
    )


@cli.command()
@click.argument("source_ids", nargs=-1)
@click.option(
    "--source-id",
    "more_ids",
    multiple=True,
    help="An item's source ID (repeatable); the same as a positional ID.",
)
@click.option(
    "--status",
    type=click.Choice(["failed", "skipped", "link-only"]),
    default="failed",
    show_default=True,
    help=(
        "Requeue items whose latest row has this status (link-only: the "
        "link-only skips, which skipped also covers)."
    ),
)
@click.option(
    "--error-like",
    default=None,
    help="Only items whose error contains this text (case-insensitive).",
)
@click.option(
    "--class",
    "error_class",
    type=click.Choice(["transient", "content", "config"]),
    default=None,
    help="Only items whose failure has this class.",
)
@click.option("--transient", is_flag=True, help="Short for --class transient.")
@click.option(
    "--since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Only items whose latest row is from this date on (YYYY-MM-DD).",
)
@click.option("--source", "source_name", default=None, help="Filter by source name.")
@click.option(
    "--all",
    "select_all",
    is_flag=True,
    help="Allow a filter that matches every item of the status.",
)
@click.option(
    "--yes",
    is_flag=True,
    help=(
        "Requeue the items. Without it, only preview. --yes selects again, so "
        "failures that arrived since the preview are included; pin "
        "--source-id to requeue exactly what you reviewed."
    ),
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def retry(
    ctx: click.Context,
    source_ids: tuple[str, ...],
    more_ids: tuple[str, ...],
    status: str,
    error_like: str | None,
    error_class: str | None,
    transient: bool,
    since: datetime | None,
    source_name: str | None,
    select_all: bool,
    yes: bool,
    output_format: str,
) -> None:
    """Requeue failed or skipped items, so the next ingest runs them again.

    Without --yes this is a dry run: it lists what would be requeued and
    writes nothing. --yes runs the selection again and requeues what it
    lists, which can include failures written since an earlier preview.
    With --yes it writes a 'queued' row for each item (the log is never
    edited), records the request in receipt.retry_requests, and
    resets each item's attempt count. A filter that would match every item
    of the status is refused unless --all is given.

    Exits 1 when an item couldn't be requeued (another process held it, or
    its state changed since it was listed).
    """
    from receipt_index.db import get_connection
    from receipt_index.repository import RetryFilter, select_retry_candidates
    from receipt_index.requeue import apply_requeue, orphan_report
    from receipt_index.store import open_store

    if transient:
        if error_class not in (None, "transient"):
            raise click.UsageError("--transient conflicts with --class")
        error_class = "transient"
    spec = RetryFilter(
        status=status,  # type: ignore[arg-type]
        source_ids=tuple(dict.fromkeys((*source_ids, *more_ids))),
        error_like=error_like or None,
        error_class=error_class,
        since=_aware(since),
        source_name=source_name,
    )
    if not spec.narrows() and not select_all:
        raise click.UsageError(
            f"This would requeue every {status} item"
            + (f" of source {source_name}" if source_name else "")
            + ". Narrow it with source IDs, --error-like, --class/--transient "
            "or --since, or pass --all."
        )

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    store = open_store(config.store)

    with get_connection(config.database.url) as conn:
        candidates = select_retry_candidates(conn, spec)
        report = orphan_report(conn, store, candidates)
        outcome = apply_requeue(conn, spec, candidates) if yes and candidates else None

    _print_retry(candidates, report, outcome, dry_run=not yes, fmt=output_format)
    if outcome is not None and outcome.not_requeued:
        sys.exit(1)


@cli.command()
@click.argument("receipt_id")
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def show(ctx: click.Context, receipt_id: str, output_format: str) -> None:
    """Show details for a specific receipt."""
    from receipt_index.db import get_connection
    from receipt_index.duplicates import merged_into_this
    from receipt_index.repository import get_receipt_by_id, with_provenance
    from receipt_index.store import rendition_ref

    config = _load_config_or_exit(ctx)

    _configure_logging(config)

    try:
        uid = UUID(receipt_id)
    except ValueError:
        click.echo(f"Error: invalid UUID: {receipt_id}", err=True)
        sys.exit(1)

    merged_here: list[UUID] = []
    with get_connection(config.database.url) as conn:
        receipt = get_receipt_by_id(conn, uid)
        if receipt is not None:
            (receipt,) = with_provenance(conn, [receipt])
            merged_here = merged_into_this(conn, receipt.id)

    if receipt is None:
        click.echo(f"Receipt not found: {receipt_id}", err=True)
        sys.exit(1)

    if output_format == "json":
        click.echo(receipt.model_dump_json(indent=2))
    else:
        click.echo(f"ID:           {receipt.id}")
        click.echo(f"Vendor:       {receipt.vendor}")
        basis = (
            "total; paid amount not known" if receipt.paid_amount is None else "paid"
        )
        click.echo(
            f"Amount:       {receipt.effective_amount} {receipt.currency} ({basis})"
        )
        paid = (
            "\u2014 (not known)"
            if receipt.paid_amount is None
            else str(receipt.paid_amount)
        )
        click.echo(f"Paid:         {paid}")
        click.echo(f"Total:        {receipt.amount}")
        if receipt.kind == "purchase":
            click.echo("Kind:         purchase")
        else:
            click.echo(f"Kind:         {receipt.kind} (counts {receipt.signed_amount})")
        if receipt.merged_into_id is not None:
            click.echo(
                f"Merged into:  {receipt.merged_into_id} (a duplicate; left out "
                "of search and totals)"
            )
        for merged in merged_here:
            click.echo(f"Merged here:  {merged}")
        for other in receipt.suspected_duplicate_of:
            click.echo(f"Possible duplicate of: {other} (see `duplicates`)")
        if receipt.refund_of is not None:
            click.echo(f"Refund of:    {receipt.refund_of}")
        if receipt.net_paid is not None and receipt.refunded:
            click.echo(
                f"Net paid:     {receipt.net_paid} (refunded {receipt.refunded})"
            )
        click.echo(f"Date:         {receipt.receipt_date}")
        desc = receipt.description or "\u2014"
        click.echo(f"Description:  {desc}")
        if receipt.order_number:
            click.echo(f"Order:        {receipt.order_number}")
        payment = _payment_line(receipt)
        if payment:
            click.echo(f"Payment:      {payment}")
        click.echo(f"Confidence:   {receipt.confidence}")
        if receipt.field_confidence:
            fields = ", ".join(
                f"{name} {value:.2f}"
                for name, value in receipt.field_confidence.items()
            )
            click.echo(f"Field Conf:   {fields}")
        if receipt.manual_fields:
            click.echo(f"Hand-edited:  {', '.join(receipt.manual_fields)}")
        click.echo(f"PDF:          {receipt.pdf_path}")
        click.echo(f"Rendition:    {rendition_ref(receipt.pdf_path)}")
        click.echo(f"Source:       {receipt.source_type} ({receipt.source_id})")
        if receipt.source_name:
            click.echo(f"Source Name:  {receipt.source_name}")
        if receipt.email_subject:
            click.echo(f"Subject:      {receipt.email_subject}")
        if receipt.email_sender:
            click.echo(f"Sender:       {receipt.email_sender}")
        if receipt.email_date:
            click.echo(f"Email Date:   {receipt.email_date.isoformat()}")
        if receipt.file_name:
            click.echo(f"File:         {receipt.file_name}")


def _concurrent_change() -> NoReturn:
    """A deadlock with another writer: Postgres rolled this change back."""
    click.echo(
        "Error: a concurrent change to the same receipts; nothing was saved. "
        "Run it again.",
        err=True,
    )
    sys.exit(1)


def _parse_receipt_id(receipt_id: str) -> UUID:
    try:
        return UUID(receipt_id)
    except ValueError:
        click.echo(f"Error: invalid UUID: {receipt_id}", err=True)
        sys.exit(1)


def _shown(value: Any) -> str:
    return "\u2014" if value is None else str(value)


def _event_summary(event: ReceiptEvent) -> str:
    """One line for an event's payload, by its type."""
    payload = event.payload
    if event.event_type == "field_edited":
        return (
            f"{payload.get('field')} {_shown(payload.get('old'))} \u2192 "
            f"{_shown(payload.get('new'))}"
        )
    if event.event_type == "manual_cleared":
        return f"{payload.get('field')} handed back to re-extraction"
    return json.dumps(payload, sort_keys=True, default=str)


@cli.command()
@click.argument("receipt_id")
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def history(ctx: click.Context, receipt_id: str, output_format: str) -> None:
    """Show a receipt's history: every change made after extraction."""
    from receipt_index.db import get_connection
    from receipt_index.repository import get_receipt_by_id, get_receipt_events

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    uid = _parse_receipt_id(receipt_id)

    with get_connection(config.database.url, read_only=True) as conn:
        receipt = get_receipt_by_id(conn, uid)
        events = [] if receipt is None else get_receipt_events(conn, uid)

    if receipt is None:
        click.echo(f"Receipt not found: {receipt_id}", err=True)
        sys.exit(1)

    if output_format == "json":
        data = [e.model_dump(mode="json") for e in events]
        click.echo(json.dumps(data, indent=2))
        return
    if not events:
        click.echo(f"No history for receipt {uid}.")
        return
    for event in events:
        when = event.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
        line = (
            f"{when}  {event.event_type:<16} "
            f"{event.actor} ({event.actor_kind})  {_event_summary(event)}"
        )
        if event.reason:
            line += f"  \u2014 {event.reason}"
        click.echo(line)


_MAX_PAID_AMOUNT = 10_000_000_000


def _paid_amount(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> Decimal | None:
    """A paid amount: 0 or more, with at most two decimal places."""
    from decimal import Decimal, InvalidOperation

    if value is None:
        return None
    try:
        amount = Decimal(value.strip())
    except InvalidOperation:
        raise click.BadParameter(f"not a decimal amount: {value}") from None
    if not amount.is_finite() or amount < 0:
        raise click.BadParameter("must be 0 or more")
    # The column is NUMERIC(12,2): at most ten digits before the point.
    if amount >= _MAX_PAID_AMOUNT:
        raise click.BadParameter(f"must be less than {_MAX_PAID_AMOUNT}")
    if amount != amount.quantize(Decimal("0.01")):
        raise click.BadParameter("at most two decimal places")
    return amount


@cli.command()
@click.argument("receipt_id")
@click.option(
    "--paid-amount",
    callback=_paid_amount,
    default=None,
    help="What was charged, or for a refund returned (0 or more).",
)
@click.option("--vendor", callback=_text_filter, default=None, help="Vendor name.")
@click.option(
    "--date",
    "receipt_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Receipt date (YYYY-MM-DD).",
)
@click.option(
    "--order-number", callback=_text_filter, default=None, help="Order number."
)
@click.option(
    "--kind",
    type=click.Choice(KINDS),
    default=None,
    help=(
        "purchase, refund, credit or statement. Refused while the receipt is "
        "in a refund link (use `refund unlink` first)."
    ),
)
@click.option(
    "--clear-manual",
    "clear",
    multiple=True,
    type=click.Choice(
        ["paid_amount", "vendor", "date", "receipt_date", "order_number", "kind"]
    ),
    help=(
        "Hand FIELD back to re-extraction (repeatable). Can't be combined "
        "with the field options."
    ),
)
@click.option("--reason", default=None, help="Why: recorded with each change.")
@click.option(
    "--as",
    "actor",
    callback=_text_filter,
    default=None,
    help="Who made the change (default: annotations.actor, else 'operator').",
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def edit(
    ctx: click.Context,
    receipt_id: str,
    paid_amount: Decimal | None,
    vendor: str | None,
    receipt_date: datetime | None,
    order_number: str | None,
    kind: str | None,
    clear: tuple[str, ...],
    reason: str | None,
    actor: str | None,
    output_format: str,
) -> None:
    """Correct a receipt's fields by hand, or hand them back.

    Each edited field is marked manual, so re-extraction leaves it alone,
    and recorded in the receipt's history with its old and new value.
    """
    from receipt_index import edits
    from receipt_index.db import get_connection

    values: dict[str, Any] = {
        name: value
        for name, value in (
            ("paid_amount", paid_amount),
            ("vendor", vendor),
            ("receipt_date", receipt_date.date() if receipt_date else None),
            ("order_number", order_number),
            ("kind", kind),
        )
        if value is not None
    }
    if clear and values:
        raise click.UsageError("--clear-manual can't be combined with field options")
    if not clear and not values:
        raise click.UsageError(
            "nothing to edit: give --paid-amount, --vendor, --date, "
            "--order-number, --kind or --clear-manual"
        )

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    uid = _parse_receipt_id(receipt_id)
    who = actor or config.annotations.actor
    reason = (reason or "").strip() or None

    try:
        with get_connection(config.database.url) as conn:
            if clear:
                result = edits.clear_manual(conn, uid, clear, actor=who, reason=reason)
            else:
                result = edits.edit_receipt(conn, uid, values, actor=who, reason=reason)
    except edits.EditError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except DeadlockDetected:
        _concurrent_change()

    if output_format == "json":
        data = {
            "receipt": result.receipt.model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in result.events],
        }
        click.echo(json.dumps(data, indent=2))
        return
    for change in result.changes:
        if change.unchanged:
            click.echo(
                f"{change.field}: {_shown(change.old)} (unchanged; now hand-set)"
            )
        else:
            click.echo(
                f"{change.field}: {_shown(change.old)} \u2192 {_shown(change.new)}"
            )
    for event in result.events:
        if event.event_type == "manual_cleared":
            click.echo(_event_summary(event))
        elif event.event_type == "refund_linked":
            click.echo(
                f"Refund {event.receipt_id} linked to purchase "
                f"{event.payload.get('purchase_id')} (rule)"
            )
    click.echo(f"Recorded as {who} (person). See `receipt-index history {uid}`.")


# The per-field confidences --confidence-below can name.
_CONFIDENCE_FIELDS = ("vendor", "amount", "paid_amount", "date")


def _parse_confidence_below(value: str | None) -> tuple[str | None, float] | None:
    """``0.6`` (overall) or ``paid_amount=0.6`` (one field's entry)."""
    if value is None:
        return None
    field_name, _, number = value.rpartition("=")
    if field_name and field_name not in _CONFIDENCE_FIELDS:
        raise click.BadParameter(
            f"unknown field {field_name!r}; use one of {', '.join(_CONFIDENCE_FIELDS)}",
            param_hint="--confidence-below",
        )
    try:
        threshold = float(number)
    except ValueError:
        raise click.BadParameter(
            f"not a number: {number!r}", param_hint="--confidence-below"
        ) from None
    if not 0.0 <= threshold <= 1.0:
        raise click.BadParameter(
            "must be between 0 and 1", param_hint="--confidence-below"
        )
    return (field_name or None, threshold)


def _source_adapter(config: AppConfig) -> Callable[[SourceConfig], SourceAdapter]:
    """A factory that builds the adapter for one configured source."""

    def build(source_config: SourceConfig) -> SourceAdapter:
        from receipt_index.adapters.imap import ImapAdapter
        from receipt_index.config import GdriveSourceConfig, ImapSourceConfig

        if isinstance(source_config, ImapSourceConfig):
            return ImapAdapter(source_config)
        if isinstance(source_config, GdriveSourceConfig):
            from receipt_index.adapters.gdrive import GdriveAdapter

            return GdriveAdapter(
                source_config, settle_seconds=config.ingest.drive_settle_seconds
            )
        raise click.ClickException(f"unknown source type: {source_config.name}")

    return build


def _extraction_agents(config: AppConfig, model: str) -> dict[str, Any]:
    """The email and document extraction agents: their prompts differ."""
    from receipt_index.extraction import (
        DOCUMENT_SYSTEM_PROMPT,
        create_extraction_agent,
    )

    return {
        "email": create_extraction_agent(api_key=config.llm.api_key, model=model),
        "document": create_extraction_agent(
            api_key=config.llm.api_key,
            model=model,
            system_prompt=DOCUMENT_SYSTEM_PROMPT,
        ),
    }


def _statement_agents(
    config: AppConfig, sources: Sequence[SourceConfig]
) -> StatementAgents | None:
    """The statement agents, when any of ``sources`` takes statements.

    Built only through ``llm.anthropic_model``, whose client never traces,
    and ``StatementAgents.create``, which refuses to build while tracing
    instrumentation is loaded.
    """
    from receipt_index.fetching import STATEMENT_INGEST_TYPE, ingest_type

    if not any(ingest_type(s) == STATEMENT_INGEST_TYPE for s in sources):
        return None
    from receipt_index.llm import anthropic_model
    from receipt_index.statements import StatementAgents

    model, settings = anthropic_model(
        api_key=config.llm.api_key, model=config.llm.model
    )
    return StatementAgents.create(model, settings)


def _cost(tokens: Decimal, price: Decimal | None) -> Decimal | None:
    from decimal import Decimal

    if price is None:
        return None
    return tokens * price / Decimal(1_000_000)


def _token_estimate(
    conn: Any, config: AppConfig, model: str, count: int
) -> dict[str, Any] | None:
    """Estimated tokens and cost of ``count`` extractions, from measured runs.

    None when nothing has been measured yet. ``cost`` is None without the
    configured prices.
    """
    from decimal import Decimal

    from receipt_index.extraction import EXTRACTION_PROMPT_VERSION
    from receipt_index.repository import token_averages

    averages = token_averages(
        conn, llm_model=model, prompt_version=EXTRACTION_PROMPT_VERSION
    )
    if averages is None:
        return None
    n = Decimal(count)
    tokens_in = n * Decimal(str(averages["avg_input"]))
    tokens_out = n * Decimal(str(averages["avg_output"]))
    cost_in = _cost(tokens_in, config.llm.price_per_mtok_input)
    cost_out = _cost(tokens_out, config.llm.price_per_mtok_output)
    return {
        "basis": averages["basis"],
        "measured_runs": int(averages["runs"]),
        "avg_input_tokens": round(float(averages["avg_input"])),
        "avg_output_tokens": round(float(averages["avg_output"])),
        "input_tokens": int(tokens_in),
        "output_tokens": int(tokens_out),
        "cost": (
            str((cost_in + cost_out).quantize(Decimal("0.0001")))
            if cost_in is not None and cost_out is not None
            else None
        ),
    }


def _echo_estimate(estimate: dict[str, Any] | None) -> None:
    """Print a ``_token_estimate`` for a dry run."""
    if estimate is None:
        click.echo("Estimate: no measured token figures yet.")
        return
    click.echo(
        f"Estimate: {estimate['input_tokens']:,} input and "
        f"{estimate['output_tokens']:,} output tokens "
        f"(averages of {estimate['measured_runs']} runs from "
        f"{estimate['basis']})."
    )
    if estimate["cost"] is not None:
        click.echo(f"Estimated cost: ${estimate['cost']}")
    else:
        click.echo(
            "Set llm.price_per_mtok_input and "
            "llm.price_per_mtok_output for a cost estimate."
        )


@cli.command()
@click.option("--source", "source_name", required=True, help="The source to backfill.")
@click.option(
    "--since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="From this day, inclusive (YYYY-MM-DD). Default: the whole source.",
)
@click.option(
    "--until",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="To this day, inclusive (YYYY-MM-DD).",
)
@click.option(
    "--originals-only",
    is_flag=True,
    help="Store the missing originals of indexed receipts; no extraction.",
)
@click.option(
    "--dry-run", is_flag=True, help="Count what a run would do, and its LLM cost."
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Max items to download: unseen items plus missing originals (with "
        "--originals-only, originals)."
    ),
)
@click.pass_context
def backfill(
    ctx: click.Context,
    source_name: str,
    since: datetime | None,
    until: datetime | None,
    originals_only: bool,
    dry_run: bool,
    limit: int | None,
) -> None:
    """Process a source's back catalogue over a date range.

    The full mode lists the source (IMAP: arrival date; Drive: created time)
    and processes every item it hasn't seen, at one LLM call each. For a
    seen IMAP receipt whose original is missing, it stores the original.
    --originals-only fetches and stores the missing originals of indexed
    receipts and makes no LLM call; a message that is gone is marked
    unavailable. Backfill takes item locks only, so it runs beside `serve`,
    and never moves the sync cursor. A re-run does nothing new.
    """
    from contextlib import ExitStack

    from receipt_index.backfill import run_backfill, run_originals_only
    from receipt_index.db import INGEST_STATEMENT_TIMEOUT_SECONDS, get_connection
    from receipt_index.fetching import SourcePool
    from receipt_index.retries import RetryPolicy
    from receipt_index.store import open_store

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    source_config = next((s for s in config.sources if s.name == source_name), None)
    if source_config is None:
        available = ", ".join(s.name for s in config.sources)
        click.echo(
            f"Error: source {source_name!r} not found in config. "
            f"Available sources: {available}",
            err=True,
        )
        sys.exit(1)
    if getattr(source_config, "documents", None) == "statements":
        # Backfill runs the receipt pipeline; a statement source's items
        # never go there.
        click.echo(
            f"Error: source {source_name!r} takes statements; backfill reads "
            "receipt sources only",
            err=True,
        )
        sys.exit(1)
    since_day = since.date() if since else None
    until_day = until.date() if until else None
    if since_day and until_day and until_day < since_day:
        raise click.BadParameter("--until is before --since")
    store = open_store(config.store)
    with get_connection(
        config.database.url,
        statement_timeout_seconds=INGEST_STATEMENT_TIMEOUT_SECONDS,
    ) as conn:
        if originals_only:
            with ExitStack() as stack:
                pool = SourcePool([source_config], _source_adapter(config), stack)
                originals = run_originals_only(
                    conn,
                    pool,
                    store=store,
                    source_name=source_name,
                    since=since_day,
                    until=until_day,
                    dry_run=dry_run,
                    limit=limit,
                )
            if dry_run:
                click.echo(f"Originals to fetch: {originals.to_fetch}")
            else:
                click.echo(
                    f"Stored: {originals.stored}  "
                    f"Unavailable: {originals.unavailable}  "
                    f"Failed: {originals.failed}  "
                    f"Locked: {originals.locked}"
                )
            if originals.ambiguous:
                click.echo(
                    f"Left pending: {originals.ambiguous} whose Message-ID more "
                    "than one message carries (see the log; resolve by hand)"
                )
            if originals.unsearchable:
                click.echo(
                    f"Left pending: {originals.unsearchable} with no Message-ID to "
                    "search for (the full mode stores these from a listing)"
                )
            if originals.drive_pending:
                click.echo(
                    f"Left pending: {originals.drive_pending} Drive originals "
                    "(no Drive store backend yet)"
                )
            if dry_run:
                click.echo("No LLM calls: --originals-only makes none.")
            if originals.source_error is not None:
                click.echo(f"Error: {originals.source_error}", err=True)
            if originals.failed or originals.source_error is not None:
                sys.exit(1)
            return

        agent = None
        if not dry_run:
            agents = _extraction_agents(config, config.llm.model)
            agent = agents["document" if source_config.type == "gdrive" else "email"]
        adapter = _source_adapter(config)(source_config)
        result = run_backfill(
            conn,
            adapter,
            store=store,
            source_name=source_name,
            source_type=source_config.type,
            llm_model=config.llm.model,
            agent=agent,
            since=since_day,
            until=until_day,
            dry_run=dry_run,
            limit=limit,
            policy=RetryPolicy.from_config(config.ingest),
        )
        if dry_run:
            click.echo(
                f"Listed: {result.listed}  Unseen: {result.unseen}  "
                f"Originals missing: {result.originals_missing}"
                + (f"  (capped by --limit {limit})" if limit is not None else "")
            )
            _echo_estimate(
                _token_estimate(conn, config, config.llm.model, result.unseen)
            )
        else:
            click.echo(
                f"Listed: {result.listed}  Processed: {result.processed}  "
                f"Skipped: {result.skipped}  Failed: {result.failed}  "
                f"Locked: {result.locked}  "
                f"Originals stored: {result.originals_stored}"
            )
    if result.source_error is not None:
        click.echo(f"Error: source {source_name}: {result.source_error}", err=True)
    if result.failed or result.originals_failed or result.source_error is not None:
        sys.exit(1)


@cli.command()
@click.option(
    "--date-from",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Receipts dated on or after this day (YYYY-MM-DD).",
)
@click.option(
    "--date-to",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Receipts dated on or before this day (YYYY-MM-DD).",
)
@click.option("--source", "source_name", default=None, help="Only this source.")
@click.option("--id", "ids", multiple=True, help="Only this receipt (repeatable).")
@click.option(
    "--model-not",
    default=None,
    help="Only receipts whose latest extraction used a different model.",
)
@click.option(
    "--confidence-below",
    default=None,
    help=(
        "Only receipts whose overall confidence is below X (`0.6`), or whose "
        "confidence for one field is below X or missing (`paid_amount=0.6`)."
    ),
)
@click.option(
    "--refund-candidates",
    is_flag=True,
    help=(
        "Only receipts still marked purchase whose subject, description, "
        "vendor or file name has a whole word such as refund, credit, return, "
        "reversal, chargeback, statement or cancelled (not 'credit card'): "
        "the back-fill for kind."
    ),
)
@click.option(
    "--all", "select_all", is_flag=True, help="Select every receipt (no filter)."
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Also replace fields an earlier extraction set (never hand-edited ones).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Include receipts already at the current prompt version and model.",
)
@click.option(
    "--input",
    "input_mode",
    type=click.Choice(["auto", "rendition"]),
    default="auto",
    help="auto: the source item, else the stored PDF. rendition: the PDF only.",
)
@click.option("--limit", type=int, default=None, help="At most N receipts.")
@click.option(
    "--dry-run", is_flag=True, help="List the selection and estimate its cost."
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def reextract(
    ctx: click.Context,
    date_from: datetime | None,
    date_to: datetime | None,
    source_name: str | None,
    ids: tuple[str, ...],
    model_not: str | None,
    confidence_below: str | None,
    refund_candidates: bool,
    select_all: bool,
    overwrite: bool,
    force: bool,
    input_mode: str,
    limit: int | None,
    dry_run: bool,
    output_format: str,
) -> None:
    """Re-extract existing receipts with the current prompt and model.

    By default a run fills only empty fields (and, on a receipt extracted
    under the old prompt, replaces its amount with the total). Fields a
    person edited are never written. Every run is recorded in the receipt's
    extraction history. Receipts already extracted at the current prompt
    version with the configured model are skipped, so an interrupted run can
    be started again with the same arguments.

    Exits 1 when any receipt failed, was below the confidence gate, was
    locked, or its source couldn't be reached.
    """
    from contextlib import ExitStack

    from receipt_index.db import get_connection
    from receipt_index.extraction import EXTRACTION_PROMPT_VERSION
    from receipt_index.fetching import SourcePool
    from receipt_index.reextract import (
        ReceiptOutcome,
        Status,
        planned_input,
        run_reextract,
    )
    from receipt_index.repository import select_for_reextract
    from receipt_index.store import open_store

    selectors = (
        date_from,
        date_to,
        source_name,
        ids,
        model_not,
        confidence_below,
        refund_candidates,
    )
    if not select_all and limit is None and not any(selectors):
        raise click.UsageError(
            "Select receipts with --date-from, --date-to, --source, --id, "
            "--model-not, --confidence-below, --refund-candidates or --limit, "
            "or pass --all."
        )
    below = _parse_confidence_below(confidence_below)
    try:
        receipt_ids = [UUID(i) for i in ids]
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--id") from None

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    model = config.llm.model

    with get_connection(config.database.url) as conn:
        receipts = select_for_reextract(
            conn,
            prompt_version=EXTRACTION_PROMPT_VERSION,
            llm_model=model,
            date_from=date_from.date() if date_from else None,
            date_to=date_to.date() if date_to else None,
            source_name=source_name,
            ids=receipt_ids,
            force=force,
            model_not=model_not,
            confidence_below=below,
            refund_candidates=refund_candidates,
            limit=limit,
        )
        conn.commit()

        with ExitStack() as stack:
            pool = (
                None
                if input_mode == "rendition"
                else SourcePool(config.sources, _source_adapter(config), stack)
            )

            if dry_run:
                planned = [(r, planned_input(pool, r)) for r in receipts]
                estimate = _token_estimate(conn, config, model, len(receipts))
                if output_format == "json":
                    click.echo(
                        json.dumps(
                            {
                                "dry_run": True,
                                "model": model,
                                "prompt_version": EXTRACTION_PROMPT_VERSION,
                                "receipts": [
                                    {
                                        "receipt_id": str(r.id),
                                        "receipt_date": r.receipt_date.isoformat(),
                                        "vendor": r.vendor,
                                        "source_name": r.source_name,
                                        "input": how,
                                    }
                                    for r, how in planned
                                ],
                                "estimate": estimate,
                            },
                            indent=2,
                        )
                    )
                    return
                for r, how in planned:
                    click.echo(
                        f"{r.id}  {r.receipt_date}  {r.vendor[:30]:<30}  "
                        f"{r.source_name}  [{how}]"
                    )
                click.echo(
                    f"\n{len(receipts)} receipt(s) selected "
                    f"(model {model}, prompt version {EXTRACTION_PROMPT_VERSION})."
                )
                _echo_estimate(estimate)
                return

            if not receipts:
                if output_format == "json":
                    click.echo(json.dumps({"receipts": [], "stopped": None}))
                else:
                    click.echo("No receipts selected.")
                return

            agents = _extraction_agents(config, model)

            def report(outcome: ReceiptOutcome) -> None:
                if output_format == "json":
                    return
                paid = outcome.extracted_paid_amount
                conf = outcome.paid_amount_confidence
                extracted = (
                    "paid ?"
                    if outcome.status in ("locked", "unavailable", "current")
                    else f"paid {paid if paid is not None else 'null'}"
                    + (f" ({conf:.2f})" if conf is not None else "")
                )
                click.echo(
                    f"{outcome.receipt_id}  {outcome.receipt_date}  "
                    f"{outcome.vendor[:30]:<30}  {outcome.status:<11} "
                    f"[{outcome.input_kind or '-'}]  {extracted}"
                )
                for name, (old, new) in outcome.changes.items():
                    click.echo(f"    {name}: {old} -> {new}")
                if outcome.kind_kept:
                    click.echo(
                        f"    kind: kept (extracted {outcome.kind_kept}); the "
                        "receipt is in a refund link, `refund unlink` it first"
                    )
                if outcome.error:
                    click.echo(f"    error: {outcome.error}")

            result = run_reextract(
                conn,
                receipts,
                pool=pool,
                store=open_store(config.store),
                agents=agents,
                llm_model=model,
                overwrite=overwrite,
                force=force,
                on_outcome=report,
            )

    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "receipts": [o.to_json() for o in result.outcomes],
                    "stopped": result.stopped,
                },
                indent=2,
            )
        )
    else:
        shown: tuple[Status, ...] = (
            "applied",
            "unchanged",
            "current",
            "gated",
            "failed",
            "locked",
            "unavailable",
        )
        counts = ", ".join(
            f"{status} {result.count(status)}"
            for status in shown
            if result.count(status)
        )
        click.echo(f"\n{len(result.outcomes)} receipt(s): {counts or 'none'}")
        if result.stopped:
            click.echo(f"Run {result.stopped}", err=True)
    if not result.ok:
        sys.exit(1)


@cli.group()
def refund() -> None:
    """Link refunds to the purchases they return money for.

    Ingest, re-extraction and `edit` link a refund by rule when exactly one
    purchase has its order number, vendor and currency and a paid amount.
    These commands link and unlink by hand, and list refunds.
    """


def _refund_actor(config: AppConfig, actor: str | None) -> str:
    return actor or config.annotations.actor


@refund.command("link")
@click.argument("refund_id")
@click.argument("purchase_id")
@click.option("--reason", default=None, help="Why: recorded with the link.")
@click.option(
    "--as",
    "actor",
    callback=_text_filter,
    default=None,
    help="Who made the change (default: annotations.actor, else 'operator').",
)
@click.pass_context
def refund_link(
    ctx: click.Context,
    refund_id: str,
    purchase_id: str,
    reason: str | None,
    actor: str | None,
) -> None:
    """Link REFUND_ID to PURCHASE_ID (same currency; refund not yet linked)."""
    from receipt_index import refunds
    from receipt_index.db import get_connection

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    refund_uid = _parse_receipt_id(refund_id)
    purchase_uid = _parse_receipt_id(purchase_id)
    who = _refund_actor(config, actor)
    try:
        with get_connection(config.database.url) as conn:
            refunds.link(
                conn,
                refund_uid,
                purchase_uid,
                actor=who,
                reason=(reason or "").strip() or None,
            )
    except refunds.RefundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except DeadlockDetected:
        _concurrent_change()
    click.echo(f"Linked refund {refund_uid} to purchase {purchase_uid} as {who}.")


@refund.command("unlink")
@click.argument("refund_id")
@click.option("--reason", default=None, help="Why: recorded with the change.")
@click.option(
    "--as",
    "actor",
    callback=_text_filter,
    default=None,
    help="Who made the change (default: annotations.actor, else 'operator').",
)
@click.pass_context
def refund_unlink(
    ctx: click.Context, refund_id: str, reason: str | None, actor: str | None
) -> None:
    """Unlink REFUND_ID from its purchase. The rule won't link it again."""
    from receipt_index import refunds
    from receipt_index.db import get_connection

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    uid = _parse_receipt_id(refund_id)
    who = _refund_actor(config, actor)
    try:
        with get_connection(config.database.url) as conn:
            refunds.unlink(conn, uid, actor=who, reason=(reason or "").strip() or None)
    except refunds.RefundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except DeadlockDetected:
        _concurrent_change()
    click.echo(f"Unlinked refund {uid} as {who}.")


@refund.command("list")
@click.option("--unlinked", is_flag=True, help="Only refunds with no purchase.")
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def refund_list(ctx: click.Context, unlinked: bool, output_format: str) -> None:
    """List refunds, newest first, with the purchase each is linked to."""
    from receipt_index.db import get_connection
    from receipt_index.repository import list_refunds

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    with get_connection(config.database.url, read_only=True) as conn:
        rows = list_refunds(conn, unlinked=unlinked)

    if output_format == "json":
        click.echo(json.dumps([r.model_dump(mode="json") for r in rows], indent=2))
        return
    if not rows:
        click.echo("No refunds found.")
        return
    click.echo(
        f"{'ID':<38} {'Date':<12} {'Vendor':<20} {'Order':<16} "
        f"{'Amount':>10}  Refund of"
    )
    click.echo("-" * 136)
    for r in rows:
        click.echo(
            f"{r.id!s:<38} "
            f"{r.receipt_date.isoformat():<12} "
            f"{_clip(r.vendor, 20):<20} "
            f"{_clip(r.order_number or '', 16):<16} "
            f"{r.effective_amount!s:>10}  "
            f"{r.refund_of or '(unlinked)'}"
        )
    click.echo(f"\n{len(rows)} refund(s).")


# ---------------------------------------------------------------------------
# Duplicates (evidence-store design, §8)
# ---------------------------------------------------------------------------

_AS_OPTION = click.option(
    "--as",
    "actor",
    callback=_text_filter,
    default=None,
    help="Who made the change (default: annotations.actor, else 'operator').",
)


def _person(config: AppConfig, actor: str | None, reason: str | None) -> Actor:
    from receipt_index.duplicates import Actor

    return Actor(actor or config.annotations.actor, "person", reason)


def _required_reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise click.BadParameter("must not be blank", param_hint="--reason")
    return reason


@cli.command("link")
@click.argument("provenance_id")
@click.argument("receipt_id")
@click.option("--reason", required=True, help="Why: recorded with the link.")
@_AS_OPTION
@click.pass_context
def link_command(
    ctx: click.Context,
    provenance_id: str,
    receipt_id: str,
    reason: str,
    actor: str | None,
) -> None:
    """Link a source item (PROVENANCE_ID) to RECEIPT_ID as a duplicate.

    If it was its receipt's last source item, that receipt is merged: kept,
    but left out of search and totals. Refused when the merged receipt has
    hand-edited fields, or is a refund linked to another purchase.
    """
    from receipt_index import duplicates
    from receipt_index.db import get_connection

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    prov = _parse_receipt_id(provenance_id)
    target = _parse_receipt_id(receipt_id)
    who = _person(config, actor, _required_reason(reason))
    try:
        with get_connection(config.database.url) as conn:
            remaining = duplicates.link(conn, prov, target, actor=who)
            conn.commit()
    except duplicates.DuplicateError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except DeadlockDetected:
        _concurrent_change()
    click.echo(f"Linked {prov} to receipt {remaining} as {who.name}.")


@cli.command("unlink")
@click.argument("provenance_id")
@click.option("--reason", required=True, help="Why: recorded with the change.")
@_AS_OPTION
@click.pass_context
def unlink_command(
    ctx: click.Context, provenance_id: str, reason: str, actor: str | None
) -> None:
    """Move a source item (PROVENANCE_ID) out to a receipt of its own.

    A receipt first made for it, and merged, is restored as it was. An item
    that joined an existing receipt at ingest is fetched from its source,
    extracted and rendered into a new receipt (one LLM call); if its source
    no longer has it, nothing changes.
    """
    from contextlib import ExitStack

    from receipt_index import duplicates
    from receipt_index.db import get_connection
    from receipt_index.extraction import DOCUMENT_SYSTEM_PROMPT, create_extraction_agent
    from receipt_index.fetching import SourceDownError, SourcePool, fetch_item
    from receipt_index.models import Provenance
    from receipt_index.pipeline import unlink_to_own_receipt
    from receipt_index.store import open_store

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    prov_id = _parse_receipt_id(provenance_id)
    who = _person(config, actor, _required_reason(reason))
    try:
        with get_connection(config.database.url) as conn:
            result = duplicates.unlink_home(conn, prov_id, actor=who)
            if result is not None:
                conn.commit()
                restored = " (restored)" if result.restored else ""
                click.echo(
                    f"Unlinked {prov_id} to receipt {result.receipt_id}{restored}"
                    f" as {who.name}."
                )
                return
            row = conn.execute(
                "SELECT * FROM receipt.provenance WHERE id = %s", (prov_id,)
            ).fetchone()
            conn.commit()
            prov = Provenance.model_validate(row)
            with ExitStack() as stack:
                pool = SourcePool(config.sources, _source_adapter(config), stack)
                try:
                    fetched = fetch_item(
                        pool,
                        source_id=prov.source_id,
                        source_name=prov.source_name,
                        source_type=prov.source_type,
                    )
                except SourceDownError as exc:
                    click.echo(f"Error: {exc}; nothing changed.", err=True)
                    sys.exit(1)
                if fetched.raw is None:
                    click.echo(
                        f"Error: {prov.source_type} item {prov.source_id} can't be"
                        " fetched from its source, so it can't be given a receipt"
                        " of its own; nothing changed.",
                        err=True,
                    )
                    sys.exit(1)
                agent = create_extraction_agent(
                    api_key=config.llm.api_key,
                    model=config.llm.model,
                    **(
                        {"system_prompt": DOCUMENT_SYSTEM_PROMPT}
                        if fetched.raw.source_type == "gdrive"
                        else {}
                    ),
                )
                receipt = unlink_to_own_receipt(
                    conn,
                    prov,
                    fetched.raw,
                    store=open_store(config.store),
                    llm_model=config.llm.model,
                    actor=who,
                    agent=agent,
                )
    except duplicates.DuplicateError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except DeadlockDetected:
        _concurrent_change()
    click.echo(
        f"Unlinked {prov_id} to new receipt {receipt.id}"
        f" ({receipt.vendor}, {receipt.effective_amount}) as {who.name}."
    )


@cli.group(invoke_without_command=True)
@click.option(
    "--status",
    type=click.Choice(["open", "linked", "dismissed", "all"]),
    default="open",
    help="Which pairs to list (default: open).",
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def duplicates(ctx: click.Context, status: str, output_format: str) -> None:
    """List suspected duplicate pairs, newest first.

    Subcommands decide a pair (link, dismiss) or scan existing receipts.
    """
    if ctx.invoked_subcommand is not None:
        return
    from receipt_index import duplicates as dup
    from receipt_index.db import get_connection

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    with get_connection(config.database.url, read_only=True) as conn:
        pairs = dup.list_candidates(
            conn, status=None if status == "all" else cast("CandidateStatus", status)
        )
    if output_format == "json":
        click.echo(
            json.dumps(
                [
                    {
                        "id": str(p.id),
                        "reason": p.reason,
                        "status": p.status,
                        "decided_by": p.decided_by,
                        "decided_at": p.decided_at.isoformat()
                        if p.decided_at
                        else None,
                        "created_at": p.created_at.isoformat(),
                        "provenance_id": str(p.provenance_id),
                        "candidate_of_provenance_id": str(p.candidate_of_provenance_id),
                        "receipt": p.receipt.model_dump(mode="json"),
                        "candidate_of_receipt": p.candidate_of_receipt.model_dump(
                            mode="json"
                        ),
                    }
                    for p in pairs
                ],
                indent=2,
            )
        )
        return
    if not pairs:
        which = "" if status == "all" else f"{status} "
        click.echo(f"No {which}duplicate pairs.")
        return
    for p in pairs:
        click.echo(f"{p.id}  {p.reason}  {p.status}")
        for label, r in (("  ", p.receipt), ("  ", p.candidate_of_receipt)):
            click.echo(
                f"{label}{r.id}  {r.receipt_date.isoformat()}  "
                f"{_clip(r.vendor, 24):<24} {r.effective_amount!s:>10} {r.currency}"
                f"  {r.kind}{'  (merged)' if r.merged_into_id else ''}"
            )
    click.echo(f"\n{len(pairs)} pair(s).")


def _decide(
    ctx: click.Context,
    pair_id: str,
    status: Literal["linked", "dismissed"],
    actor: str | None,
) -> None:
    from receipt_index import duplicates as dup
    from receipt_index.db import get_connection

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    uid = _parse_receipt_id(pair_id)
    who = _person(config, actor, None)
    try:
        with get_connection(config.database.url) as conn:
            remaining = dup.decide_candidate(conn, uid, status, actor=who)
            conn.commit()
    except dup.DuplicateError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except DeadlockDetected:
        _concurrent_change()
    if remaining is None:
        click.echo(f"Dismissed pair {uid} as {who.name}.")
    else:
        click.echo(f"Linked pair {uid} into receipt {remaining} as {who.name}.")


@duplicates.command("link")
@click.argument("pair_id")
@_AS_OPTION
@click.pass_context
def duplicates_link(ctx: click.Context, pair_id: str, actor: str | None) -> None:
    """Link a flagged pair: the newer receipt merges into the older."""
    _decide(ctx, pair_id, "linked", actor)


@duplicates.command("dismiss")
@click.argument("pair_id")
@_AS_OPTION
@click.pass_context
def duplicates_dismiss(ctx: click.Context, pair_id: str, actor: str | None) -> None:
    """Dismiss a flagged pair: two real receipts. It isn't flagged again."""
    _decide(ctx, pair_id, "dismissed", actor)


@duplicates.command("scan")
@click.option("--dry-run", is_flag=True, help="Report what it would do; write nothing.")
@click.option(
    "--window-days",
    type=click.IntRange(min=0),
    default=DEFAULT_WINDOW_DAYS,
    show_default=True,
    help="How many days apart two receipts' dates may be and still be flagged.",
)
@click.pass_context
def duplicates_scan(ctx: click.Context, dry_run: bool, window_days: int) -> None:
    """Run the duplicate rules over existing receipts, oldest first.

    No LLM calls. Identical files (rules 1 and 2) are found only among items
    whose originals were kept; order-number and look-alike rules (3 and 4)
    cover every receipt. The older receipt of a pair remains.
    """
    from receipt_index import duplicates as dup
    from receipt_index.db import get_connection

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    try:
        with get_connection(config.database.url) as conn:
            result = dup.scan(conn, window_days=window_days)
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
    except DeadlockDetected:
        _concurrent_change()
    verb = "Would link" if dry_run else "Linked"
    for merged, kept, method in result.links:
        click.echo(f"{verb} {merged} into {kept} ({method})")
    verb_f = "Would flag" if dry_run else "Flagged"
    for receipt, other, reason in result.flags:
        click.echo(f"{verb_f} {receipt} with {other} ({reason})")
    linked = ", ".join(f"{k} {v}" for k, v in sorted(result.linked.items())) or "none"
    flagged = ", ".join(f"{k} {v}" for k, v in sorted(result.flagged.items())) or "none"
    if result.no_provenance:
        click.echo(
            f"\n{len(result.no_provenance)} receipt(s) have no provenance record,"
            " so the rules can't link them (re-run migration 000018, which"
            " backfills it): " + ", ".join(str(r) for r in result.no_provenance)
        )
    prefix = "Dry run: nothing written. " if dry_run else ""
    click.echo(f"\n{prefix}Linked: {linked}. Flagged: {flagged}.")


@cli.command("mcp")
@click.option(
    "--access-log",
    "access_log_path",
    type=click.Path(dir_okay=False),
    default=None,
    envvar="RECEIPT_INDEX_MCP_ACCESS_LOG",
    help=(
        "JSON-lines file recording every file fetch and withheld statement read "
        "[default: $XDG_STATE_HOME/receipt-index/mcp-access.jsonl]."
    ),
)
@click.option(
    "--max-content-bytes",
    type=click.IntRange(min=0),
    default=None,
    help="Largest PDF returned as content [default: 10 MiB].",
)
@click.pass_context
def mcp_command(
    ctx: click.Context,
    access_log_path: str | None,
    max_content_bytes: int | None,
) -> None:
    """Run a read-only MCP server over stdio.

    Connects with RECEIPT_INDEX_MCP_DATABASE_URL, or database.url from the
    config when it is unset, and refuses to start unless that role is
    read-only. Logs go to stderr; stdout carries the MCP protocol.
    """
    from receipt_index.mcp_server import (
        DEFAULT_MAX_CONTENT_BYTES,
        AccessLog,
        AccessLogError,
        RoleCheckError,
        build_server,
        default_access_log_path,
        startup_check,
    )
    from receipt_index.store import open_store

    config = _load_config_or_exit(ctx)

    _configure_logging(config)

    import os

    import psycopg

    database_url = (
        os.environ.get("RECEIPT_INDEX_MCP_DATABASE_URL") or config.database.url
    )
    access_log = AccessLog(
        Path(access_log_path) if access_log_path else default_access_log_path()
    )

    try:
        startup_check(database_url)
        access_log.check_writable()
    except (RoleCheckError, AccessLogError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except psycopg.OperationalError as exc:
        click.echo(f"Error: cannot connect to the database: {exc}", err=True)
        sys.exit(1)
    except psycopg.Error as exc:
        click.echo(f"Error: startup check failed: {exc}", err=True)
        sys.exit(1)

    server = build_server(
        database_url=database_url,
        store=open_store(config.store),
        access_log=access_log,
        max_content_bytes=(
            DEFAULT_MAX_CONTENT_BYTES
            if max_content_bytes is None
            else max_content_bytes
        ),
    )
    logger.info("MCP server ready on stdio (access log: %s)", access_log.path)
    server.run("stdio")


@cli.group()
def store() -> None:
    """Manage the evidence store's records."""


@store.command("backfill-hashes")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Read and hash the renditions, and write nothing.",
)
@click.pass_context
def store_backfill_hashes(ctx: click.Context, dry_run: bool) -> None:
    """Record the hash and location of every rendition not yet recorded.

    For each receipt with no recorded rendition, reads its local file, and
    records its stored file, its local location and its rendition file ID.
    Run once after upgrading; a second run finds nothing to do. Exits 1 if
    any receipt's file is missing or unreadable.
    """
    from receipt_index.db import get_connection
    from receipt_index.hash_backfill import backfill_rendition_hashes
    from receipt_index.store import open_store

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    evidence = open_store(config.store)
    with get_connection(config.database.url) as conn:
        report = backfill_rendition_hashes(conn, evidence, dry_run=dry_run)
    verb = "Would record" if dry_run else "Recorded"
    click.echo(
        f"{verb} {report.hashed} rendition(s);"
        f" {report.shared} share bytes with another receipt."
    )
    for receipt_id, why in report.missing:
        click.echo(f"Not recorded: {receipt_id}: {why}", err=True)
    if not report.ok:
        click.echo(
            f"{len(report.missing)} receipt(s) not recorded;"
            " restore their files, or resolve the conflict named, and run again.",
            err=True,
        )
        sys.exit(1)


@cli.group()
def auth() -> None:
    """Manage authentication for external services."""


@auth.command()
@click.option(
    "--client-credentials",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to Google OAuth2 client_secret.json file.",
)
def gdrive(client_credentials: str) -> None:
    """Authorize Google Drive access (one-time setup).

    Opens a browser for OAuth2 authorization, then outputs the token
    JSON for storing as the GDRIVE_TOKEN_JSON environment variable.
    """
    from receipt_index.auth import AuthError, run_gdrive_auth

    try:
        token_json = run_gdrive_auth(client_credentials)
    except AuthError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo("\nAuthorization successful. Token JSON:\n")
    click.echo(token_json)
    click.echo(
        "\nStore this value as the GDRIVE_TOKEN_JSON environment variable."
        "\nFor example, add to your .env file:"
        "\n"
        f"\n  GDRIVE_TOKEN_JSON='{token_json}'"
    )


# ---------------------------------------------------------------------------
# statements (plan U8)
# ---------------------------------------------------------------------------


@contextmanager
def _statement_snapshot(database_url: str) -> Iterator[Any]:
    """One read-only, repeatable-read transaction for a statement read.

    READ ONLY is set on the transaction itself, not only as the session's
    default, so no write can run inside it.
    """
    import psycopg

    from receipt_index import db

    with db.get_connection(database_url, read_only=True) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        yield conn


def _rerun_agents(config: AppConfig) -> StatementAgents:
    """The statement agents, built as ``serve`` builds them.

    Only through ``llm.anthropic_model``, whose client never traces, and
    ``StatementAgents.create``, which refuses to build while tracing
    instrumentation is loaded.
    """
    from receipt_index.llm import anthropic_model
    from receipt_index.statements import StatementAgents

    model, settings = anthropic_model(
        api_key=config.llm.api_key, model=config.llm.model
    )
    return StatementAgents.create(model, settings)


@cli.group()
def statements() -> None:
    """Bank, card and loan statements: list, show, rerun and republish.

    Text is shown as stored: masked, so at most the last four digits of a
    card or account number. `show` checks it once more before printing, as
    the MCP tools do, and refuses (withheld) a revision with a field past
    the mask's bound.
    """


_DASH = "\u2014"
_STATEMENT_STATUSES = ("complete", "incomplete", "held", "rejected")
_OUTPUT_OPTION = click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
# The database's bigint: a revision past it never reaches a query.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    sys.exit(1)


def _statement_uuid(value: str) -> UUID:
    """A statement ID argument; the input is never echoed back."""
    try:
        return UUID(value)
    except ValueError:
        _fail("Error: the statement ID is not a UUID.")


def _cli_client() -> dict[str, str | None]:
    """Who ran the command, for the access log."""
    import getpass

    try:
        user: str | None = getpass.getuser()
    except (KeyError, OSError):
        user = None
    return {"name": "receipt-index", "user": user}


def _field(label: str, value: object) -> str:
    """A labelled line, values aligned in one column."""
    return f"{label + ':':<19}{value}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


@statements.command("list")
@click.option(
    "--status",
    "statuses",
    multiple=True,
    type=click.Choice(_STATEMENT_STATUSES),
    help="Only statements with this status (repeatable).",
)
@click.option(
    "--since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Only statements whose current revision was extracted on or after "
    "this day (YYYY-MM-DD, UTC).",
)
@_OUTPUT_OPTION
@click.pass_context
def statements_list(
    ctx: click.Context,
    statuses: tuple[str, ...],
    since: datetime | None,
    output_format: str,
) -> None:
    """List statements at their current revision, in feed order.

    Read-only. Prints no free text: IDs, revisions, dates, statuses and
    types only. JSON entries are the read contract's change-feed entries.
    """
    from receipt_index.statement_contract import (
        STATEMENT_CONTRACT_VERSION,
        change_out,
    )
    from receipt_index.statement_repository import list_statements

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    with _statement_snapshot(config.database.url) as conn:
        listed = list_statements(
            conn,
            statuses=statuses,
            since=None if since is None else since.replace(tzinfo=UTC),
        )

    if output_format == "json":
        items = [change_out(s.change) for s in listed]
        envelope = page_envelope(
            "statements", items, total=len(items), limit=None, offset=0
        )
        click.echo(
            json.dumps(
                {"contract_version": STATEMENT_CONTRACT_VERSION, **envelope}, indent=2
            )
        )
        return
    if not listed:
        click.echo("No statements.")
        return
    click.echo(
        f"{'ID':<36}  {'REVISION':>16}  {'EXTRACTED':<10}  {'STATUS':<10}  "
        f"{'TYPE':<20}  HOLD"
    )
    for s in listed:
        click.echo(
            f"{s.change.statement_id!s:<36}  {s.change.revision:>16}  "
            f"{s.extracted_at.astimezone(UTC).date().isoformat():<10}  "
            f"{s.change.status:<10}  {s.change.document_type:<20}  "
            f"{s.hold_reason or ''}".rstrip()
        )
    click.echo(_plural(len(listed), "statement"))


def _withhold(statement_id: str, revision: int, failed: list[str]) -> NoReturn:
    """Refuse `statements show`: log it, record it in the access log, exit 1.

    Field names only, never the text. The refusal stands even if the access
    log can't be written.
    """
    from receipt_index.access_log import (
        AccessLog,
        AccessLogError,
        configured_access_log_path,
        withheld_entry,
    )

    names = ", ".join(failed)
    logger.warning(
        "statements show withheld statement %s revision %s: %s failed the read check",
        statement_id,
        revision,
        names,
    )
    click.echo(
        f"withheld: statement {statement_id} revision {revision}: a stored "
        f"field failed the read check ({names}). Treat this revision as held "
        "for review; `statements rerun` re-extracts it.",
        err=True,
    )
    entry = withheld_entry(
        tool="cli statements show",
        statement_id=statement_id,
        revision=revision,
        account_ordinal=None,
        fields=failed,
        client=_cli_client(),
    )
    try:
        AccessLog(configured_access_log_path()).record(entry)
    except AccessLogError as exc:
        logger.error("%s", exc)
        click.echo(f"Warning: {exc}", err=True)
    sys.exit(1)


def _read_revision(conn: Any, uid: UUID, revision: int | None) -> dict[str, Any]:
    """A revision of a statement, or exit 1 naming what doesn't exist."""
    from receipt_index.statement_repository import get_revision, statement_exists

    rev = get_revision(conn, uid, revision)
    if rev is not None:
        return rev
    if revision is None or not statement_exists(conn, uid):
        _fail(f"Error: no statement {uid}")
    _fail(f"Error: no revision {revision} of statement {uid}")


def _money_or_dash(value: str | None) -> str:
    return _DASH if value is None else value


def _print_account(account: dict[str, Any]) -> None:
    last4 = account["last4"] or _DASH
    held = ", held by the statement" if account["held_by_statement"] else ""
    click.echo(
        f"  [{account['ordinal']}] {account['account_type']} {last4} "
        f"{account['currency'] or _DASH}: {account['status']}{held}"
    )
    click.echo(f"      Institution: {account['institution'] or _DASH}")
    period = (
        f"{account['period_start']} to {account['period_end']}"
        if account["period_start"] or account["period_end"]
        else _DASH
    )
    click.echo(f"      Period: {period}")
    click.echo(
        f"      Balances: beginning {_money_or_dash(account['beginning_balance'])}, "
        f"ending {_money_or_dash(account['ending_balance'])}"
    )
    click.echo(
        f"      Lines: {account['line_count']} ({account['line_order'] or 'no order'})"
    )
    if account["failure_class"] is not None:
        pages = (
            f", pages {account['first_page']}-{account['last_page']}"
            if account["first_page"] is not None
            else ""
        )
        difference = (
            f", difference {account['difference']}"
            if account["difference"] is not None
            else ""
        )
        click.echo(f"      Failure: {account['failure_class']}{difference}{pages}")
    if account["section_totals"]:
        totals = "; ".join(
            f"{t['section']} {t['amount']}" for t in account["section_totals"]
        )
        click.echo(f"      Section totals: {totals}")
    if account["column_totals"]:
        columns = "; ".join(
            f"{t['section'] or 'whole account'} credits "
            f"{_money_or_dash(t['credits'])}, debits {_money_or_dash(t['debits'])}"
            for t in account["column_totals"]
        )
        click.echo(f"      Column totals: {columns}")
    if account["loan"] is not None:
        figures = ", ".join(
            f"{name.replace('_', ' ')} {_money_or_dash(value)}"
            for name, value in account["loan"].items()
        )
        click.echo(f"      Loan: {figures}")
    click.echo(f"      Digest: {account['content_digest']}")


def _print_statement(data: dict[str, Any]) -> None:
    """A revision in text, from its contract shape (already checked)."""
    current = (
        "current" if data["is_current"] else f"current is {data['current_revision']}"
    )
    status = data["status"]
    if data["hold_reason"]:
        status += f" ({data['hold_reason']})"
    doc_type = data["document_type"]
    if data["rejected_bound"]:
        doc_type += f" ({data['rejected_bound']})"
    period = (
        f"{data['period_start']} to {data['period_end']}"
        if data["period_start"] or data["period_end"]
        else _DASH
    )
    extractor = data["extractor"]
    click.echo(_field("Statement", f"{data['statement_id']}"))
    click.echo(_field("SHA-256", f"{data['source_sha256']}"))
    click.echo(_field("Revision", f"{data['revision']} ({current})"))
    click.echo(_field("Extracted", f"{data['extracted_at']}"))
    click.echo(_field("Type", f"{doc_type}"))
    click.echo(_field("Status", f"{status}"))
    click.echo(_field("Period", f"{period}"))
    click.echo(_field("Pages", f"{_shown(data['pages'])}"))
    click.echo(_field("Printed accounts", f"{_shown(data['printed_account_count'])}"))
    click.echo(
        _field(
            "Extractor",
            f"{_shown(extractor['model'])}, prompt "
            f"{_shown(extractor['prompt_version'])}, first pass "
            f"{_shown(extractor['first_pass_version'])}",
        )
    )
    click.echo(_field("Masked digit runs", f"{data['masked_runs']}"))
    click.echo(_field("Accounts", f"{len(data['accounts'])}"))
    for account in data["accounts"]:
        _print_account(account)


@statements.command("show")
@click.argument("statement_id")
@click.option(
    "--revision",
    type=int,
    default=None,
    help="Revision number; the current revision when left out.",
)
@_OUTPUT_OPTION
@click.pass_context
def statements_show(
    ctx: click.Context,
    statement_id: str,
    revision: int | None,
    output_format: str,
) -> None:
    """Show one revision of a statement and its accounts (not their lines).

    Read-only. JSON is the read contract's per-statement shape, the same as
    the MCP tool get_statement. Before printing, the stored text is checked
    against the mask's bound and the stored totals against the money
    format, as the MCP tools check them: if a field fails, nothing of the
    revision is printed, the field names go to stderr and the access log,
    and the command exits 1 (withheld).
    """
    from receipt_index.statement_contract import (
        account_out,
        statement_out,
        statement_read_failures,
    )
    from receipt_index.statement_repository import get_accounts

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    uid = _statement_uuid(statement_id)
    if revision is not None and not _INT64_MIN <= revision <= _INT64_MAX:
        _fail("Error: --revision is out of range.")
    with _statement_snapshot(config.database.url) as conn:
        rev = _read_revision(conn, uid, revision)
        accounts = [account_out(row) for row in get_accounts(conn, rev["revision_id"])]

    failed = statement_read_failures(accounts)
    if failed:
        _withhold(str(uid), rev["revision"], failed)
    data = statement_out(rev, accounts)
    if output_format == "json":
        click.echo(json.dumps(data, indent=2))
    else:
        _print_statement(data)


@statements.command("file")
@click.argument("statement_id")
@_OUTPUT_OPTION
@click.pass_context
def statements_file(ctx: click.Context, statement_id: str, output_format: str) -> None:
    """Print where a statement's original PDF is stored: its path only.

    Never the file's content: the command doesn't open it. The original is
    unmasked, so each use is recorded in the access log first; if the log
    can't be written, no path is printed and the command exits 1.
    """
    from datetime import datetime as _datetime

    from receipt_index.access_log import (
        AccessLog,
        AccessLogError,
        configured_access_log_path,
    )
    from receipt_index.statement_repository import original_location
    from receipt_index.store import InvalidRefError, open_store

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    uid = _statement_uuid(statement_id)
    with _statement_snapshot(config.database.url) as conn:
        location = original_location(conn, uid)
    if location is None:
        _fail(f"Error: no statement {uid}")
    ref: str | None = location["ref"]
    if ref is None:
        _fail(f"Error: statement {uid} has no stored original.")
    try:
        path = open_store(config.store).local_path(ref)
    except InvalidRefError:
        _fail(f"Error: the original of statement {uid} isn't in the local store.")
    sha = bytes(location["source_sha256"]).hex()
    entry = {
        "ts": _datetime.now(UTC).isoformat(),
        "tool": "cli statements file",
        "statement_id": str(uid),
        "source_sha256": sha,
        "outcome": "path",
        "client": _cli_client(),
    }
    try:
        AccessLog(configured_access_log_path()).record(entry)
    except AccessLogError as exc:
        _fail(f"Error: {exc}; no path printed.")
    exists = path.exists()
    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "statement_id": str(uid),
                    "source_sha256": sha,
                    "ref": ref,
                    "path": str(path),
                    "exists": exists,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"{path}" + ("" if exists else "  (missing)"))


def _run_summary(rev: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A revision with no free text: IDs, enums, counts and dates only."""
    from receipt_index.statement_contract import timestamp

    return {
        "revision": rev["revision"],
        "extracted_at": timestamp(rev["created_at"]),
        "status": rev["status"],
        "hold_reason": rev["hold_reason"],
        "document_type": rev["document_type"],
        "rejected_bound": rev["rejected_bound"],
        "masked_runs": rev["masked_runs"],
        "input_tokens": rev["input_tokens"],
        "output_tokens": rev["output_tokens"],
        "accounts": [
            {
                "ordinal": a["ordinal"],
                "account_type": a["account_type"],
                "last4": a["last4"],
                "status": a["status"],
                "failure_class": a["failure_class"],
                "held_by_statement": a["held_by_statement"],
                "line_count": a["line_count"],
            }
            for a in rows
        ],
    }


def _read_summary(config: AppConfig, uid: UUID) -> tuple[dict[str, Any], str]:
    """The current revision's summary and the statement's hash, or exit 1."""
    from receipt_index.statement_repository import get_accounts

    with _statement_snapshot(config.database.url) as conn:
        rev = _read_revision(conn, uid, None)
        rows = get_accounts(conn, rev["revision_id"])
    return _run_summary(rev, rows), bytes(rev["source_sha256"]).hex()


def _with_reason(value: str, reason: str | None) -> str:
    return value if reason is None else f"{value} ({reason})"


def _print_run_accounts(summary: dict[str, Any]) -> None:
    for a in summary["accounts"]:
        status = _with_reason(a["status"], a["failure_class"])
        if a["held_by_statement"]:
            status += ", held by the statement"
        click.echo(
            f"  [{a['ordinal']}] {a['account_type']} {a['last4'] or _DASH}: "
            f"{status}, {_plural(a['line_count'], 'line')}"
        )


def _becomes_current(uid: UUID, revision: int) -> str:
    return (
        "The new revision becomes current whatever its result: if it is held "
        "or rejected, it is still current. To change that, rerun again; "
        f"revision {revision} stays readable with "
        f"`statements show {uid} --revision {revision}`."
    )


@statements.command("rerun")
@click.argument("statement_id")
@click.option(
    "--yes",
    is_flag=True,
    help="Run it. Without it, only show the current revision and what a "
    "rerun would do.",
)
@_OUTPUT_OPTION
@click.pass_context
def statements_rerun(
    ctx: click.Context, statement_id: str, yes: bool, output_format: str
) -> None:
    """Extract a statement again from its stored original, as a new revision.

    Without --yes this is a dry run: it shows the current revision's
    figures and makes no model call. With --yes it reads the stored
    original, runs the first pass and one call per account with the
    configured model, the same mask and gates as intake, and appends a new
    revision, which becomes current whatever its result. Every earlier
    revision is kept unchanged.

    Prints no free text: run `statements show` for the new revision's text.
    """
    from receipt_index.statement_extraction import StatementCallError
    from receipt_index.statements import (
        StatementItemError,
        StatementRerunError,
        rerun_statement,
    )
    from receipt_index.store import open_store

    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    uid = _statement_uuid(statement_id)
    previous, sha = _read_summary(config, uid)

    if not yes:
        if output_format == "json":
            click.echo(
                json.dumps(
                    {"statement_id": str(uid), "dry_run": True, "current": previous},
                    indent=2,
                )
            )
            return
        click.echo(_field("Statement", f"{uid}"))
        click.echo(_field("SHA-256", f"{sha}"))
        click.echo(
            _field(
                "Current revision",
                f"{previous['revision']} (extracted {previous['extracted_at']})",
            )
        )
        status = _with_reason(previous["status"], previous["hold_reason"])
        click.echo(_field("Status", f"{status}"))
        doc_type = _with_reason(previous["document_type"], previous["rejected_bound"])
        click.echo(_field("Type", doc_type))
        click.echo(_field("Masked digit runs", f"{previous['masked_runs']}"))
        click.echo(
            _field(
                "Last run's tokens",
                f"{previous['input_tokens']:,} input, "
                f"{previous['output_tokens']:,} output",
            )
        )
        click.echo(_field("Accounts", f"{len(previous['accounts'])}"))
        _print_run_accounts(previous)
        click.echo("")
        click.echo(
            "Dry run: nothing was changed. With --yes, the rerun reads the "
            "stored original, calls the model (a first pass, then one call per "
            "account, at most 8) and appends a new revision."
        )
        click.echo(_becomes_current(uid, previous["revision"]))
        return

    import psycopg

    from receipt_index.db import get_connection

    try:
        agents = _rerun_agents(config)
        with get_connection(config.database.url) as conn:
            rerun_statement(
                conn,
                open_store(config.store),
                uid,
                agents=agents,
                llm_model=config.llm.model,
            )
    except StatementRerunError as exc:
        _fail(f"Error: {exc}")
    except (StatementItemError, StatementCallError) as exc:
        # Both carry no statement text: a fixed message, or a kind and class.
        _fail(f"Error: the rerun failed: {exc}; nothing was written.")
    except psycopg.Error as exc:
        # By class only: a constraint's detail can quote a row's values.
        _fail(
            f"Error: the database refused the rerun ({type(exc).__name__}); "
            "nothing was written."
        )
    current, _ = _read_summary(config, uid)
    changed = current["masked_runs"] != previous["masked_runs"]

    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "statement_id": str(uid),
                    "dry_run": False,
                    "previous": previous,
                    "current": current,
                    "masked_runs_changed": changed,
                },
                indent=2,
            )
        )
        return
    click.echo(
        f"Rerun of statement {uid}: revision {previous['revision']} -> "
        f"{current['revision']}, now current."
    )
    click.echo(
        _field(
            "Status",
            f"{_with_reason(previous['status'], previous['hold_reason'])} -> "
            f"{_with_reason(current['status'], current['hold_reason'])}",
        )
    )
    click.echo(
        _field("Type", f"{previous['document_type']} -> {current['document_type']}")
    )
    click.echo(
        _field(
            "Masked digit runs",
            f"{previous['masked_runs']} -> {current['masked_runs']} "
            f"({'changed' if changed else 'unchanged'})",
        )
    )
    click.echo(
        _field(
            "Tokens",
            f"{current['input_tokens']:,} input, {current['output_tokens']:,} output",
        )
    )
    click.echo(f"Accounts in revision {current['revision']}:")
    _print_run_accounts(current)
    if current["status"] in ("held", "rejected"):
        click.echo(
            f"This run is {current['status']}, and it is current all the same: "
            "held or rejected, it is still current. Rerun again, or read the "
            f"earlier revision with `statements show {uid} --revision "
            f"{previous['revision']}`."
        )


@statements.command("republish")
@click.argument("statement_ids", nargs=-1)
@click.option("--all", "select_all", is_flag=True, help="Every statement.")
@click.option(
    "--yes",
    is_flag=True,
    help="Republish them. Without it, only list what would be republished.",
)
@_OUTPUT_OPTION
@click.pass_context
def statements_republish(
    ctx: click.Context,
    statement_ids: tuple[str, ...],
    select_all: bool,
    yes: bool,
    output_format: str,
) -> None:
    """Give statements a fresh place in the change feed, with no new revision.

    The restore runbook's last step: after a restore, a statement's place in
    the feed can sit below a cursor a client already holds, and this moves
    it past every cursor. Each statement's revision and content stay as
    they are. Without --yes this is a dry run and writes nothing. With
    --yes each statement is republished in its own short transaction, so an
    interrupted run is safe to repeat. Every ID is checked before anything
    is written.
    """
    from receipt_index.db import get_connection
    from receipt_index.statement_repository import feed_positions, republish_each

    if bool(statement_ids) == select_all:
        msg = "Give statement IDs or --all, not both."
        raise click.UsageError(msg)
    config = _load_config_or_exit(ctx)
    _configure_logging(config)
    ids: list[UUID] = []
    for n, value in enumerate(statement_ids, start=1):
        try:
            ids.append(UUID(value))
        except ValueError:
            _fail(f"Error: statement ID {n} is not a UUID; nothing was republished.")
    with _statement_snapshot(config.database.url) as conn:
        positions = feed_positions(conn, None if select_all else ids)
    missing = sorted({*ids} - {p["statement_id"] for p in positions}, key=str)
    if missing:
        _fail(
            "Error: no statement "
            + ", ".join(str(m) for m in missing)
            + "; nothing was republished."
        )

    new_seqs: list[int | None] = [None] * len(positions)
    if yes:
        with get_connection(config.database.url) as conn:
            new_seqs = list(
                republish_each(conn, [p["statement_id"] for p in positions])
            )
    entries = [
        {
            "statement_id": str(p["statement_id"]),
            "revision": p["revision"],
            "change_seq": p["change_seq"],
            "new_change_seq": seq,
        }
        for p, seq in zip(positions, new_seqs, strict=True)
    ]

    if output_format == "json":
        click.echo(
            json.dumps(
                {"dry_run": not yes, "statements": entries, "total": len(entries)},
                indent=2,
            )
        )
        return
    for e in entries:
        moved = (
            f"{e['change_seq']}"
            if e["new_change_seq"] is None
            else f"{e['change_seq']} -> {e['new_change_seq']}"
        )
        click.echo(
            f"  {e['statement_id']}  revision {e['revision']}  feed position {moved}"
        )
    if yes:
        click.echo(f"Republished {_plural(len(entries), 'statement')}.")
    else:
        click.echo(
            f"Dry run: nothing was changed. {_plural(len(entries), 'statement')} "
            "would be republished; pass --yes to republish them."
        )
