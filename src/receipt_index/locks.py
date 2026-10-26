"""Postgres advisory locks for sources and items.

Two kinds of session-level advisory lock keep ingest runs apart (see the
always-on ingest design, section 1, Locking):

- A **source lock** is held by whoever advances a source's cursor, on a
  dedicated connection, for as long as the holder runs.
- An **item lock** is held while one item is processed, on the processing
  connection. After taking it, the holder re-reads the item's state and acts
  only if it still calls for that action.

A session lock lasts exactly as long as its session: it survives a
transaction rollback, and disappears silently if the connection drops.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Self

import psycopg

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

logger = logging.getLogger(__name__)


class SourceLockLostError(Exception):
    """The connection holding a source lock has gone, and the lock with it."""


def lock_key(kind: str, name: str) -> int:
    """Return the advisory-lock key for a source or item name.

    The key is the first 8 bytes of a SHA-256 digest read as a *signed*
    integer, so it always fits Postgres ``bigint``; an unsigned value of 2^63
    or more would be sent as ``numeric``, which no advisory-lock function
    accepts. The ``kind`` prefix keeps source keys and item keys apart.
    """
    digest = hashlib.sha256(f"receipt-index:{kind}:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class SourceLock:
    """The lock held by whoever advances one source's cursor.

    It lives on its own autocommit connection, so it never sits idle in a
    transaction for the length of a run.
    """

    def __init__(self, database_url: str, source_name: str) -> None:
        self.database_url = database_url
        self.source_name = source_name
        self.key = lock_key("source", source_name)
        self._conn: psycopg.Connection[Any] | None = None

    def acquire(self) -> bool:
        """Try to take the lock without waiting. Return whether it was taken."""
        conn = psycopg.connect(self.database_url, autocommit=True)
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (self.key,)).fetchone()
        if row is None or not row[0]:
            conn.close()
            return False
        self._conn = conn
        return True

    def check(self) -> None:
        """Raise ``SourceLockLostError`` unless the lock's connection is still alive."""
        if self._conn is None or self._conn.closed:
            raise SourceLockLostError(
                f"source lock for {self.source_name!r} is not held"
            )
        try:
            self._conn.execute("SELECT 1")
        except psycopg.Error as exc:
            raise SourceLockLostError(
                f"lost the source lock for {self.source_name!r}: {exc}"
            ) from exc

    def release(self) -> None:
        """Release the lock and close its connection. Safe to call twice."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (self.key,))
        except psycopg.Error:
            logger.debug("Source lock connection already gone", exc_info=True)
        finally:
            conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


@contextmanager
def item_lock(conn: psycopg.Connection[Any], source_id: str) -> Iterator[bool]:
    """Try to lock one item on ``conn``; yield whether the lock was taken.

    The lock is released on exit. A rollback inside the block doesn't release
    it, because advisory session locks outlive transactions.
    """
    key = lock_key("item", source_id)
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (key,)).fetchone()
    locked = bool(row is not None and row["locked"])
    conn.commit()
    try:
        yield locked
    finally:
        if locked:
            _unlock_item(conn, key)


def _unlock_item(conn: psycopg.Connection[Any], key: int) -> None:
    try:
        conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        conn.commit()
    except psycopg.Error:
        # An aborted transaction refuses every statement: roll back and retry.
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        conn.commit()
