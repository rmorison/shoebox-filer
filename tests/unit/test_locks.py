"""Tests for receipt_index.locks (no database: see the integration tests)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import psycopg
import pytest

from receipt_index.locks import SourceLock, SourceLockLostError, item_lock, lock_key


def _high_bit_name() -> str:
    """A name whose digest has the high bit set (an unsigned key >= 2^63)."""
    import hashlib

    for i in range(1000):
        name = f"source-{i}"
        if hashlib.sha256(f"receipt-index:source:{name}".encode()).digest()[0] >= 128:
            return name
    raise AssertionError("no high-bit name found")


class TestLockKey:
    def test_high_bit_digest_gives_a_negative_bigint(self) -> None:
        key = lock_key("source", _high_bit_name())
        assert -(2**63) <= key < 0

    def test_keys_always_fit_bigint(self) -> None:
        for i in range(500):
            assert -(2**63) <= lock_key("item", f"<m-{i}@example.com>") < 2**63

    def test_kinds_are_kept_apart(self) -> None:
        assert lock_key("source", "x") != lock_key("item", "x")

    def test_stable(self) -> None:
        assert lock_key("source", "personal-email") == lock_key(
            "source", "personal-email"
        )


def _conn(locked: bool) -> MagicMock:
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"locked": locked}
    return conn


class TestItemLock:
    def test_taken_and_released(self) -> None:
        conn = _conn(True)
        with item_lock(conn, "<a@example.com>") as locked:
            assert locked is True
        statements = [c.args[0] for c in conn.execute.call_args_list]
        assert "pg_try_advisory_lock" in statements[0]
        assert "pg_advisory_unlock" in statements[-1]
        key = lock_key("item", "<a@example.com>")
        assert conn.execute.call_args_list[-1].args[1] == (key,)

    def test_not_taken_is_not_released(self) -> None:
        conn = _conn(False)
        with item_lock(conn, "<a@example.com>") as locked:
            assert locked is False
        statements = [c.args[0] for c in conn.execute.call_args_list]
        assert not any("unlock" in s for s in statements)

    def test_released_after_an_aborted_transaction(self) -> None:
        conn = _conn(True)
        unlock_calls = 0

        def execute(sql: str, params: object = None) -> MagicMock:
            nonlocal unlock_calls
            result = MagicMock()
            result.fetchone.return_value = {"locked": True}
            if "unlock" in sql:
                unlock_calls += 1
                if unlock_calls == 1:
                    raise psycopg.errors.InFailedSqlTransaction("aborted")
            return result

        conn.execute.side_effect = execute
        with item_lock(conn, "<a@example.com>"):
            pass
        conn.rollback.assert_called_once()
        assert unlock_calls == 2


class TestSourceLock:
    @patch("receipt_index.locks.psycopg.connect")
    def test_acquire_uses_an_autocommit_connection(self, connect: MagicMock) -> None:
        connect.return_value.execute.return_value.fetchone.return_value = (True,)
        lock = SourceLock("postgresql://db", "personal-email")

        assert lock.acquire() is True
        assert connect.call_args.kwargs == {"autocommit": True}
        lock.release()
        connect.return_value.close.assert_called_once()

    @patch("receipt_index.locks.psycopg.connect")
    def test_not_acquired_closes_the_connection(self, connect: MagicMock) -> None:
        connect.return_value.execute.return_value.fetchone.return_value = (False,)
        lock = SourceLock("postgresql://db", "personal-email")
        assert lock.acquire() is False
        connect.return_value.close.assert_called_once()
        with pytest.raises(SourceLockLostError):
            lock.check()

    @patch("receipt_index.locks.psycopg.connect")
    def test_check_raises_when_the_connection_is_gone(self, connect: MagicMock) -> None:
        conn = connect.return_value
        conn.closed = False
        conn.execute.return_value.fetchone.return_value = (True,)
        lock = SourceLock("postgresql://db", "personal-email")
        lock.acquire()

        lock.check()  # alive
        conn.execute.side_effect = psycopg.OperationalError("terminated")
        with pytest.raises(SourceLockLostError, match="terminated"):
            lock.check()
