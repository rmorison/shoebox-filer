"""CLI entry point for receipt-index."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import click
from dotenv import load_dotenv

from receipt_index.config import AppConfig, ConfigError, load_config

# Load .env before any config interpolation
load_dotenv()

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from receipt_index.adapters.base import SourceAdapter

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

    Each source is read from where the last run stopped. A source that a
    running process holds is skipped; the exit code is then 3.
    """
    from receipt_index.adapters.imap import ImapAdapter
    from receipt_index.config import GdriveSourceConfig, ImapSourceConfig
    from receipt_index.db import get_connection
    from receipt_index.extraction import create_extraction_agent
    from receipt_index.locks import SourceLock
    from receipt_index.pipeline import IngestResult, run_ingest, warn_stale_started
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

    # Create extraction agents — email and document prompts differ
    from receipt_index.extraction import DOCUMENT_SYSTEM_PROMPT

    email_agent = create_extraction_agent(
        api_key=config.llm.api_key,
        model=config.llm.model,
    )
    document_agent = create_extraction_agent(
        api_key=config.llm.api_key,
        model=config.llm.model,
        system_prompt=DOCUMENT_SYSTEM_PROMPT,
    )

    totals = IngestResult()
    source_results: list[tuple[str, IngestResult]] = []
    locked_sources: list[str] = []
    failed_sources: list[str] = []

    with get_connection(config.database.url) as conn:
        warn_stale_started(conn)

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

            with get_connection(config.database.url) as conn:
                result = run_ingest(
                    conn=conn,
                    adapter=adapter,
                    store=store,
                    source_name=source_config.name,
                    source_type=source_config.type,
                    agent=agent,
                    llm_model=config.llm.model,
                    dry_run=dry_run,
                    limit=limit,
                    source_lock=source_lock,
                )

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
    output_format: str,
) -> None:
    """Search indexed receipts."""
    from decimal import Decimal, InvalidOperation

    from receipt_index.db import get_connection
    from receipt_index.repository import search_receipts

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

    with get_connection(config.database.url) as conn:
        results = search_receipts(
            conn,
            vendor=vendor,
            amount=amount_d,
            amount_min=amount_min_d,
            amount_max=amount_max_d,
            date_from=date_from.date() if date_from else None,
            date_to=date_to.date() if date_to else None,
            source_name=source_name,
        )

    if output_format == "json":
        data = [r.model_dump(mode="json") for r in results]
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        if not results:
            click.echo("No receipts found.")
            return
        # Amount is the effective amount (paid when known, else the total).
        click.echo(
            f"{'ID':<38} {'Date':<12} {'Vendor':<20} {'Amount':>10} {'Total':>10}"
        )
        click.echo("-" * 93)
        for r in results:
            vendor_display = (
                r.vendor[:19] + "\u2026" if len(r.vendor) > 20 else r.vendor
            )
            click.echo(
                f"{r.id!s:<38} "
                f"{r.receipt_date.isoformat():<12} "
                f"{vendor_display:<20} "
                f"{r.effective_amount!s:>10} "
                f"{r.amount!s:>10}"
            )
        click.echo(f"\n{len(results)} receipt(s) found.")


@cli.command()
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def failures(ctx: click.Context, output_format: str) -> None:
    """List failed ingest attempts."""
    from receipt_index.db import get_connection
    from receipt_index.repository import get_ingest_failures

    config = _load_config_or_exit(ctx)

    _configure_logging(config)

    with get_connection(config.database.url) as conn:
        results = get_ingest_failures(conn)

    if output_format == "json":
        data = [r.model_dump(mode="json") for r in results]
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        if not results:
            click.echo("No failed ingests.")
            return
        click.echo(f"{'Date':<22} {'Sender':<30} {'Subject':<40} {'Error':<30}")
        click.echo("-" * 124)
        for r in results:
            email_date = (
                r.email_date.strftime("%Y-%m-%d %H:%M") if r.email_date else "\u2014"
            )
            sender = (r.email_sender or "\u2014")[:29]
            subject = (r.email_subject or "\u2014")[:39]
            error = (r.error_message or "\u2014")[:29]
            click.echo(f"{email_date:<22} {sender:<30} {subject:<40} {error:<30}")
        click.echo(f"\n{len(results)} failed ingest(s).")


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
    from receipt_index.repository import get_receipt_by_id
    from receipt_index.store import rendition_ref

    config = _load_config_or_exit(ctx)

    _configure_logging(config)

    try:
        uid = UUID(receipt_id)
    except ValueError:
        click.echo(f"Error: invalid UUID: {receipt_id}", err=True)
        sys.exit(1)

    with get_connection(config.database.url) as conn:
        receipt = get_receipt_by_id(conn, uid)

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
        click.echo(f"Date:         {receipt.receipt_date}")
        desc = receipt.description or "\u2014"
        click.echo(f"Description:  {desc}")
        click.echo(f"Confidence:   {receipt.confidence}")
        if receipt.field_confidence:
            fields = ", ".join(
                f"{name} {value:.2f}"
                for name, value in receipt.field_confidence.items()
            )
            click.echo(f"Field Conf:   {fields}")
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


@cli.command("mcp")
@click.option(
    "--access-log",
    "access_log_path",
    type=click.Path(dir_okay=False),
    default=None,
    envvar="RECEIPT_INDEX_MCP_ACCESS_LOG",
    help=(
        "JSON-lines file recording every file fetch "
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
