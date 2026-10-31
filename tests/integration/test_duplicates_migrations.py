"""Migration 000021: duplicate candidates, link history and the merge columns.

Runs the real migration files, grants included, in a scratch database (the
``scratch`` fixture from the 000011 tests, which creates and drops it), so it
never touches the shared ``receipt_index_dev`` schema.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from .test_extractions_migrations import (  # noqa: F401 (scratch is a fixture)
    _migration,
    _receipt,
    _run,
    scratch,
)

pytestmark = pytest.mark.integration

Scratch = tuple[psycopg.Connection[dict[str, Any]], str]


def _exists(conn: psycopg.Connection[Any], name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS r", (name,)).fetchone()
    return row is not None and row["r"] is not None


def _merged_columns(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE"
        " table_schema = 'receipt' AND table_name = 'receipts'"
        " AND column_name IN ('merged_into_id', 'merged_at')"
    ).fetchall()
    return {r["column_name"] for r in rows}


def _net(conn: psycopg.Connection[Any]) -> set[Any]:
    return {
        r["receipt_id"]
        for r in conn.execute("SELECT receipt_id FROM receipt.v_receipt_net")
    }


def test_up_down_up_over_existing_rows(scratch: Scratch) -> None:  # noqa: F811
    conn, _ = scratch
    kept = _receipt(conn, "<kept@example.com>")
    merged = _receipt(conn, "<merged@example.com>")
    conn.execute(
        "UPDATE receipt.receipts SET merged_into_id = %s, merged_at = now()"
        " WHERE id = %s",
        (kept, merged),
    )
    conn.commit()
    assert _net(conn) == {kept}

    _run(conn, _migration("000021", "down"))
    assert not _exists(conn, "receipt.duplicate_candidates")
    assert not _exists(conn, "receipt.provenance_link_history")
    assert _merged_columns(conn) == set()
    # 000016's view, without the merge conditions.
    assert _net(conn) == {kept, merged}

    _run(conn, _migration("000021", "up"))
    assert _exists(conn, "receipt.duplicate_candidates")
    assert _exists(conn, "receipt.provenance_link_history")
    assert _merged_columns(conn) == {"merged_into_id", "merged_at"}


def test_merged_at_goes_with_merged_into(scratch: Scratch) -> None:  # noqa: F811
    conn, _ = scratch
    a = _receipt(conn, "<a@example.com>")
    b = _receipt(conn, "<b@example.com>")
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE receipt.receipts SET merged_into_id = %s WHERE id = %s", (a, b)
        )
    conn.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE receipt.receipts SET merged_into_id = id, merged_at = now()"
            " WHERE id = %s",
            (a,),
        )
    conn.rollback()


def test_the_write_role_cannot_change_history(scratch: Scratch) -> None:  # noqa: F811
    conn, _ = scratch
    rows = conn.execute(
        "SELECT privilege_type FROM information_schema.role_table_grants"
        " WHERE grantee = 'receipt_index_dev_write'"
        " AND table_name = 'provenance_link_history'"
    ).fetchall()
    assert {r["privilege_type"] for r in rows} == {"SELECT"}
