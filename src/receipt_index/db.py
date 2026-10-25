"""Database connection helper."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


def get_connection(database_url: str) -> psycopg.Connection[dict[str, object]]:
    """Create and return a new database connection.

    Parameters
    ----------
    database_url:
        PostgreSQL connection URL.
    """
    return psycopg.connect(database_url, row_factory=dict_row)
