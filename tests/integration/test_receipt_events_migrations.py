"""Migration 000015: order and payment columns, receipt_events and its guard.

Runs the real migration files, grants included, in a scratch database (the
``scratch`` fixture from the 000011 tests, which creates and drops it), so it
never touches the shared ``receipt_index_dev`` schema. The guard must hold for
the role that owns the table, which no grant can stop.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql

from .test_extractions_migrations import (  # noqa: F401 (scratch is a fixture)
    _migration,
    _receipt,
    _run,
    scratch,
)

pytestmark = pytest.mark.integration

Scratch = tuple[psycopg.Connection[dict[str, Any]], str]


def _event(conn: psycopg.Connection[Any], receipt_id: Any, **overrides: Any) -> Any:
    values = {
        "event_type": "field_edited",
        "payload": '{"field": "paid_amount", "old": null, "new": "12.00"}',
        "actor": "operator",
        "actor_kind": "person",
    }
    values.update(overrides)
    row = conn.execute(
        "INSERT INTO receipt.receipt_events (receipt_id, event_type, payload,"
        " actor, actor_kind) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (
            receipt_id,
            values["event_type"],
            values["payload"],
            values["actor"],
            values["actor_kind"],
        ),
    ).fetchone()
    assert row is not None
    return row["id"]


def _count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT count(*) AS n FROM receipt.receipt_events").fetchone()
    assert row is not None
    return int(row["n"])


_CHANGES = {
    "update": "UPDATE receipt.receipt_events SET actor = 'someone else'",
    "delete": "DELETE FROM receipt.receipt_events",
    "truncate": "TRUNCATE receipt.receipt_events",
}


class TestUpAndDown:
    def test_up_down_up_over_existing_rows(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        receipt_id = _receipt(conn, "<keep@example.com>")
        conn.execute(
            "UPDATE receipt.receipts SET order_number = 'A-100',"
            " payment_method = 'card', payment_last4 = '1234' WHERE id = %s",
            (receipt_id,),
        )
        _event(conn, receipt_id)
        conn.commit()

        _run(conn, _migration("000015", "down"))
        row = conn.execute(
            "SELECT to_regclass('receipt.receipt_events') AS t,"
            " to_regprocedure('receipt.forbid_history_change()') AS f"
        ).fetchone()
        # The guard function stays: 000011 owns it, and extractions uses it.
        assert row == {"t": None, "f": "receipt.forbid_history_change()"}
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE"
            " table_schema = 'receipt' AND table_name = 'receipts' AND column_name"
            " IN ('order_number', 'payment_method', 'payment_last4', 'payer')"
        ).fetchall()
        assert columns == []
        kept = conn.execute(
            "SELECT count(*) AS n FROM receipt.receipts WHERE id = %s", (receipt_id,)
        ).fetchone()
        assert kept == {"n": 1}

        _run(conn, _migration("000015", "up"))
        row = conn.execute(
            "SELECT order_number, payment_last4 FROM receipt.receipts WHERE id = %s",
            (receipt_id,),
        ).fetchone()
        assert row == {"order_number": None, "payment_last4": None}
        _event(conn, receipt_id)
        conn.commit()
        assert _count(conn) == 1


class TestAppendOnly:
    @pytest.mark.parametrize("change", sorted(_CHANGES))
    def test_refused_for_the_superuser(
        self,
        scratch: Scratch,  # noqa: F811
        change: str,
    ) -> None:
        conn, _ = scratch
        _event(conn, _receipt(conn, "<a@example.com>"))
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(_CHANGES[change])
        conn.rollback()
        assert _count(conn) == 1

    @pytest.mark.parametrize("change", sorted(_CHANGES))
    def test_refused_for_the_owning_role(
        self,
        scratch: Scratch,  # noqa: F811
        change: str,
    ) -> None:
        conn, owner = scratch
        _event(conn, _receipt(conn, "<b@example.com>"))
        ident = sql.Identifier(owner)
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA receipt TO {}").format(ident))
        # As in production, one role owns every table, receipts included
        # (the guard reads receipts to tell a cascade from a direct delete).
        for table in ("receipts", "receipt_events"):
            conn.execute(
                sql.SQL("ALTER TABLE receipt.{} OWNER TO {}").format(
                    sql.Identifier(table), ident
                )
            )
        conn.commit()

        conn.execute(sql.SQL("SET ROLE {}").format(ident))
        owns = conn.execute(
            "SELECT tableowner = current_user AS owns FROM pg_tables"
            " WHERE schemaname = 'receipt' AND tablename = 'receipt_events'"
        ).fetchone()
        assert owns == {"owns": True}
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(_CHANGES[change])
        conn.rollback()
        conn.execute("RESET ROLE")
        assert _count(conn) == 1

    def test_deleting_the_receipt_cascades(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        gone = _receipt(conn, "<gone@example.com>")
        kept = _receipt(conn, "<kept@example.com>")
        _event(conn, gone)
        _event(conn, kept)
        conn.commit()

        conn.execute("DELETE FROM receipt.receipts WHERE id = %s", (gone,))
        conn.commit()
        rows = conn.execute("SELECT receipt_id FROM receipt.receipt_events").fetchall()
        assert rows == [{"receipt_id": kept}]

    def test_insert_is_allowed(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        receipt_id = _receipt(conn, "<c@example.com>")
        _event(conn, receipt_id)
        _event(conn, receipt_id, event_type="manual_cleared", payload="{}")
        _event(conn, receipt_id, event_type="refund_linked", actor_kind="rule")
        conn.commit()
        assert _count(conn) == 3


class TestChecks:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"event_type": "field_deleted"},
            {"actor_kind": "robot"},
        ],
    )
    def test_event_checks(
        self,
        scratch: Scratch,  # noqa: F811
        overrides: dict[str, str],
    ) -> None:
        conn, _ = scratch
        receipt_id = _receipt(conn, "<d@example.com>")
        with pytest.raises(psycopg.errors.CheckViolation):
            _event(conn, receipt_id, **overrides)
        conn.rollback()

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("payment_method", "cheque"),
            ("payment_last4", "123"),
            ("payment_last4", "12345"),
            ("payment_last4", "4111111111111111"),
            ("payment_last4", "12a4"),
        ],
    )
    def test_receipt_checks(
        self,
        scratch: Scratch,  # noqa: F811
        column: str,
        value: str,
    ) -> None:
        conn, _ = scratch
        receipt_id = _receipt(conn, "<e@example.com>")
        conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                sql.SQL("UPDATE receipt.receipts SET {} = %s WHERE id = %s").format(
                    sql.Identifier(column)
                ),
                (value, receipt_id),
            )
        conn.rollback()
