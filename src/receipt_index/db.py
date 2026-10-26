"""Database connection helper."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

# Server-side default for every transaction on the session: any INSERT,
# UPDATE, DELETE or DDL fails with "cannot execute ... in a read-only
# transaction", whatever the role's privileges.
_READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"


def get_connection(
    database_url: str, *, read_only: bool = False
) -> psycopg.Connection[dict[str, object]]:
    """Create and return a new database connection.

    Parameters
    ----------
    database_url:
        PostgreSQL connection URL.
    read_only:
        When true, the session starts with ``default_transaction_read_only``
        on, so Postgres rejects any write made over this connection. This
        replaces any ``options`` parameter given in the URL.
    """
    if read_only:
        return psycopg.connect(
            database_url, row_factory=dict_row, options=_READ_ONLY_OPTIONS
        )
    return psycopg.connect(database_url, row_factory=dict_row)
