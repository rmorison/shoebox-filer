"""The duplicate rules' decisions, without a database (evidence-store design, §8).

``decide`` runs one query per rule; a scripted connection answers each from
the rule's SQL, so these tests check the order the rules run in, what each
result leads to (join, flag, or nothing), and which rules are skipped. The
SQL itself runs against Postgres in tests/integration/test_duplicates.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from receipt_index import duplicates
from receipt_index.duplicates import Decision, ItemFacts, decide, merge_blocker
from receipt_index.models import Receipt

REAL_DUPLICATES = True  # these tests exercise the rules themselves

A = UUID("019572a0-0000-7000-8000-00000000000a")
B = UUID("019572a0-0000-7000-8000-00000000000b")


class _Conn:
    """Answers each rule's query with scripted rows, and records which ran."""

    def __init__(self, **rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.ran: list[str] = []

    def execute(self, sql: str, params: Any = None) -> _Conn:
        if "p.source_sha256 = ANY" in sql:
            rule = "rule1"
        elif "AS guarded" in sql:
            rule = "rule2"
        elif "r.order_number" in sql:
            rule = "rule3"
        else:
            rule = "rule4"
        self.ran.append(rule)
        self._last = self.rows.get(rule, [])
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self._last[0] if self._last else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._last


def _facts(**kw: Any) -> ItemFacts:
    values: dict[str, Any] = {
        "kind": "purchase",
        "currency": "USD",
        "vendor": "Acme Parking (U.S.), LLC",
        "effective_amount": Decimal("12.00"),
        "paid_amount": Decimal("12.00"),
        "order_number": None,
        "receipt_date": date(2026, 10, 14),
    }
    values.update(kw)
    return ItemFacts(**values)


def _decide(conn: _Conn, facts: ItemFacts) -> Decision:
    return decide(conn, facts)  # type: ignore[arg-type]


class TestRuleOrder:
    def test_rule_1_joins_and_nothing_else_runs(self) -> None:
        conn = _Conn(rule1=[{"id": A}])
        d = _decide(conn, _facts(source_hashes=(b"x" * 32,)))
        assert (d.join, d.method, d.flags) == (A, "original_hash", [])
        assert conn.ran == ["rule1"]

    def test_no_hashes_skip_rules_1_and_2(self) -> None:
        conn = _Conn()
        _decide(conn, _facts())
        assert conn.ran == ["rule4"]

    def test_rule_2_joins_only_past_its_guard(self) -> None:
        conn = _Conn(rule2=[{"id": A, "guarded": True}])
        d = _decide(conn, _facts(doc_hashes=(b"d" * 32,)))
        assert (d.join, d.method) == (A, "document_hash")
        assert conn.ran == ["rule2"]

    def test_rule_2_flags_a_guard_conflict_and_goes_on(self) -> None:
        conn = _Conn(rule2=[{"id": A, "guarded": False}], rule4=[{"id": B}])
        d = _decide(conn, _facts(doc_hashes=(b"d" * 32,)))
        assert d.join is None
        assert d.flags == [(A, "document_hash_conflict"), (B, "metadata")]


class TestRule3:
    @pytest.mark.parametrize("paid", [None, Decimal("0.00")])
    def test_no_paid_amount_skips_the_rule(self, paid: Decimal | None) -> None:
        conn = _Conn(rule3=[{"id": A, "same_date": True}])
        d = _decide(conn, _facts(order_number="O-1", paid_amount=paid))
        assert d.join is None
        assert "rule3" not in conn.ran

    def test_no_order_number_skips_the_rule(self) -> None:
        conn = _Conn(rule3=[{"id": A, "same_date": True}])
        _decide(conn, _facts())
        assert "rule3" not in conn.ran

    def test_the_same_date_joins(self) -> None:
        conn = _Conn(rule3=[{"id": A, "same_date": True}])
        d = _decide(conn, _facts(order_number="O-1"))
        assert (d.join, d.method) == (A, "order_number")

    def test_another_date_flags(self) -> None:
        conn = _Conn(rule3=[{"id": A, "same_date": False}], rule4=[{"id": A}])
        d = _decide(conn, _facts(order_number="O-1"))
        assert d.join is None
        # One row per receipt: the first reason found is the one kept.
        assert d.flags == [(A, "order_number_conflict")]

    def test_a_join_drops_an_earlier_flag_on_the_same_receipt(self) -> None:
        conn = _Conn(
            rule2=[{"id": A, "guarded": False}],
            rule3=[{"id": A, "same_date": True}],
        )
        d = _decide(conn, _facts(order_number="O-1", doc_hashes=(b"d" * 32,)))
        assert (d.join, d.flags) == (A, [])


class TestDecisionFlag:
    def test_never_flags_the_receipt_it_joins(self) -> None:
        d = Decision(join=A, method="order_number")
        d.flag(A, "metadata")
        assert d.flags == []

    def test_one_flag_per_receipt(self) -> None:
        d = Decision()
        d.flag(A, "document_hash_conflict")
        d.flag(A, "metadata")
        d.flag(B, "metadata")
        assert d.flags == [(A, "document_hash_conflict"), (B, "metadata")]


def _receipt(**kw: Any) -> Receipt:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    values: dict[str, Any] = {
        "id": uuid4(),
        "source_id": f"<{uuid4().hex}@example.com>",
        "source_type": "imap",
        "source_name": "mail",
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("12.00"),
        "currency": "USD",
        "receipt_date": date(2026, 10, 14),
        "description": None,
        "confidence": 0.9,
        "pdf_path": "renditions/x.pdf",
        "email_subject": None,
        "email_sender": None,
        "email_date": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(kw)
    return Receipt.model_validate(values)


class TestMergeBlocker:
    def test_a_plain_merge_is_allowed(self) -> None:
        assert merge_blocker(_receipt(), _receipt()) is None

    def test_different_kinds(self) -> None:
        assert "refund" in (merge_blocker(_receipt(kind="refund"), _receipt()) or "")

    def test_hand_edits(self) -> None:
        blocker = merge_blocker(_receipt(manual_fields=["vendor"]), _receipt())
        assert blocker is not None and "hand-edited fields (vendor)" in blocker

    def test_a_refund_linked_elsewhere(self) -> None:
        purchase = uuid4()
        emptied = _receipt(kind="refund", refund_of=purchase)
        assert merge_blocker(emptied, _receipt(kind="refund")) is not None
        same = _receipt(kind="refund", refund_of=purchase)
        assert merge_blocker(emptied, same) is None


def test_the_default_window_is_three_days() -> None:
    assert duplicates.DEFAULT_WINDOW_DAYS == 3
