"""Migration 000016: kind, refund_of, signed_amount and the v_receipt_net view.

Runs the real migration files, grants included, in a scratch database (the
``scratch`` fixture from the 000011 tests, which creates and drops it), so it
never touches the shared ``receipt_index_dev`` schema.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
import pytest

from receipt_index.models import Receipt

from .test_extractions_migrations import (  # noqa: F401 (scratch is a fixture)
    _migration,
    _receipt,
    _run,
    scratch,
)

pytestmark = pytest.mark.integration

Scratch = tuple[psycopg.Connection[dict[str, Any]], str]


def _set(conn: psycopg.Connection[Any], receipt_id: Any, **values: Any) -> None:
    for column, value in values.items():
        conn.execute(
            f"UPDATE receipt.receipts SET {column} = %s WHERE id = %s",
            (value, receipt_id),
        )


def _row(conn: psycopg.Connection[Any], receipt_id: Any) -> Any:
    return conn.execute(
        "SELECT * FROM receipt.receipts WHERE id = %s", (receipt_id,)
    ).fetchone()


class TestUpAndDown:
    def test_up_down_up_over_existing_rows(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        purchase = _receipt(conn, "<p@example.com>")
        refund = _receipt(conn, "<r@example.com>")
        _set(conn, refund, kind="refund", refund_of=purchase)
        conn.commit()

        _run(conn, _migration("000016", "down"))
        row = conn.execute(
            "SELECT to_regclass('receipt.v_receipt_net') AS v"
        ).fetchone()
        assert row == {"v": None}
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE"
            " table_schema = 'receipt' AND table_name = 'receipts' AND column_name"
            " IN ('kind', 'refund_of', 'signed_amount')"
        ).fetchall()
        assert columns == []
        kept = conn.execute("SELECT count(*) AS n FROM receipt.receipts").fetchone()
        assert kept == {"n": 2}

        _run(conn, _migration("000016", "up"))
        # Existing rows come back as purchases, unlinked, counting for.
        row = _row(conn, refund)
        assert row["kind"] == "purchase"
        assert row["refund_of"] is None
        assert row["signed_amount"] == Decimal("12.00")


class TestSignedAmount:
    @pytest.mark.parametrize(
        ("kind", "paid", "signed"),
        [
            ("purchase", Decimal("10.00"), Decimal("10.00")),
            ("purchase", None, Decimal("12.00")),
            ("refund", Decimal("10.00"), Decimal("-10.00")),
            ("refund", None, Decimal("-12.00")),
            ("refund", Decimal("0.00"), Decimal("0.00")),
            ("credit", Decimal("3.00"), Decimal("-3.00")),
            ("statement", Decimal("99.00"), Decimal("0.00")),
        ],
    )
    def test_column_matches_the_model(
        self,
        scratch: Scratch,  # noqa: F811
        kind: str,
        paid: Decimal | None,
        signed: Decimal,
    ) -> None:
        conn, _ = scratch
        receipt_id = _receipt(conn, f"<{kind}-{paid}@example.com>")
        _set(conn, receipt_id, kind=kind, paid_amount=paid)
        conn.commit()
        row = _row(conn, receipt_id)
        assert row["signed_amount"] == signed
        assert str(row["signed_amount"]) == str(signed)
        model = Receipt.model_validate(row)
        assert model.signed_amount == row["signed_amount"]

    def test_follows_an_update(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        receipt_id = _receipt(conn, "<u@example.com>")
        _set(conn, receipt_id, kind="refund", paid_amount=Decimal("4.00"))
        conn.commit()
        assert _row(conn, receipt_id)["signed_amount"] == Decimal("-4.00")

    def test_cannot_be_written(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        receipt_id = _receipt(conn, "<w@example.com>")
        with pytest.raises(psycopg.errors.GeneratedAlways):
            _set(conn, receipt_id, signed_amount=Decimal("1.00"))
        conn.rollback()


class TestChecks:
    def test_unknown_kind(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        receipt_id = _receipt(conn, "<k@example.com>")
        with pytest.raises(psycopg.errors.CheckViolation):
            _set(conn, receipt_id, kind="other")
        conn.rollback()

    def test_only_a_refund_has_refund_of(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        purchase = _receipt(conn, "<p1@example.com>")
        credit = _receipt(conn, "<c1@example.com>")
        _set(conn, credit, kind="credit")
        with pytest.raises(psycopg.errors.CheckViolation):
            _set(conn, credit, refund_of=purchase)
        conn.rollback()

    def test_a_linked_refund_keeps_its_kind(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        purchase = _receipt(conn, "<p2@example.com>")
        refund = _receipt(conn, "<r2@example.com>")
        _set(conn, refund, kind="refund", refund_of=purchase)
        conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation):
            _set(conn, refund, kind="purchase")
        conn.rollback()

    def test_not_its_own_refund(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        refund = _receipt(conn, "<r3@example.com>")
        _set(conn, refund, kind="refund")
        with pytest.raises(psycopg.errors.CheckViolation):
            _set(conn, refund, refund_of=refund)
        conn.rollback()

    def test_deleting_the_purchase_unlinks(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        purchase = _receipt(conn, "<p4@example.com>")
        refund = _receipt(conn, "<r4@example.com>")
        _set(conn, refund, kind="refund", refund_of=purchase)
        conn.commit()
        conn.execute("DELETE FROM receipt.receipts WHERE id = %s", (purchase,))
        conn.commit()
        assert _row(conn, refund)["refund_of"] is None


class TestReceiptNet:
    def test_two_partial_refunds(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        purchase = _receipt(conn, "<p5@example.com>")
        _set(conn, purchase, paid_amount=Decimal("100.00"))
        for n, paid in enumerate((Decimal("30.00"), Decimal("20.00"))):
            refund = _receipt(conn, f"<r5-{n}@example.com>")
            _set(conn, refund, kind="refund", paid_amount=paid, refund_of=purchase)
        unlinked = _receipt(conn, "<r5-x@example.com>")
        _set(conn, unlinked, kind="refund", paid_amount=Decimal("5.00"))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM receipt.v_receipt_net WHERE receipt_id = %s", (purchase,)
        ).fetchone()
        assert row == {
            "receipt_id": purchase,
            "paid": Decimal("100.00"),
            "refunded": Decimal("50.00"),
            "net_paid": Decimal("50.00"),
            "refund_count": 2,
        }

    def test_only_purchases(self, scratch: Scratch) -> None:  # noqa: F811
        conn, _ = scratch
        refund = _receipt(conn, "<r6@example.com>")
        statement = _receipt(conn, "<s6@example.com>")
        _set(conn, refund, kind="refund")
        _set(conn, statement, kind="statement")
        plain = _receipt(conn, "<p6@example.com>")
        conn.commit()
        rows = conn.execute(
            "SELECT receipt_id, refunded, net_paid FROM receipt.v_receipt_net"
        ).fetchall()
        assert rows == [
            {
                "receipt_id": plain,
                "refunded": Decimal("0"),
                "net_paid": Decimal("12.00"),
            }
        ]
