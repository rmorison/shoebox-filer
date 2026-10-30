"""Database connection helper."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

# Server-side default for every transaction on the session: any INSERT,
# UPDATE, DELETE or DDL fails with "cannot execute ... in a read-only
# transaction", whatever the role's privileges.
_READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"

# The statement timeout of the ingest connections (`ingest`, `serve`), so a
# hung query raises QueryCanceled (transient) instead of holding the item and
# its source lock (always-on ingest design, section 1).
INGEST_STATEMENT_TIMEOUT_SECONDS = 60


def get_connection(
    database_url: str,
    *,
    read_only: bool = False,
    statement_timeout_seconds: float | None = None,
) -> psycopg.Connection[dict[str, object]]:
    """Create and return a new database connection.

    Parameters
    ----------
    database_url:
        PostgreSQL connection URL.
    read_only:
        When true, the session starts with ``default_transaction_read_only``
        on, so Postgres rejects any write made over this connection.
    statement_timeout_seconds:
        When set, every statement on the session is cancelled after this
        long (``statement_timeout``).

    Either option replaces any ``options`` parameter given in the URL.
    """
    options: list[str] = []
    if read_only:
        options.append(_READ_ONLY_OPTIONS)
    if statement_timeout_seconds is not None:
        millis = int(statement_timeout_seconds * 1000)
        options.append(f"-c statement_timeout={millis}")
    if options:
        return psycopg.connect(
            database_url, row_factory=dict_row, options=" ".join(options)
        )
    return psycopg.connect(database_url, row_factory=dict_row)
