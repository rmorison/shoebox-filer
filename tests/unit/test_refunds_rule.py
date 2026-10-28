"""The refund linking rule's locking (receipt data-model design, §6).

Candidates are read without a lock; only the one purchase the rule will link
is locked, and re-checked, so a concurrent kind change can't leave a refund
linked to a non-purchase.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from receipt_index import refunds
from receipt_index.models import Receipt, ReceiptEvent

if TYPE_CHECKING:
    import pytest

_NOW = datetime(2026, 1, 15, tzinfo=UTC)


def _refund() -> Receipt:
    return Receipt.model_validate(
        {
            "id": uuid4(),
            "source_id": "<r@example.com>",
            "source_type": "imap",
            "source_name": "personal-email",
            "vendor": "Acme Parking (U.S.), LLC",
            "amount": Decimal("25.00"),
            "paid_amount": Decimal("25.00"),
            "currency": "USD",
            "receipt_date": date(2026, 1, 15),
            "description": None,
            "confidence": 0.9,
            "order_number": "12345",
            "kind": "refund",
            "pdf_path": "2026/01/x.pdf",
            "email_subject": None,
            "email_sender": None,
            "email_date": None,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    )


class _Repo:
    def __init__(self, matches: list[UUID], still_purchase: bool) -> None:
        self.matches = matches
        self.still_purchase = still_purchase
        self.locked: list[UUID] = []
        self.linked: list[tuple[UUID, UUID | None]] = []

    def find_refund_purchases(self, conn: Any, refund: Receipt) -> list[UUID]:
        return list(self.matches)

    def lock_if_purchase(self, conn: Any, purchase_id: UUID) -> bool:
        self.locked.append(purchase_id)
        return self.still_purchase

    def was_unlinked(self, conn: Any, refund_id: UUID) -> bool:
        return False

    def set_refund_of(self, conn: Any, refund_id: UUID, purchase: UUID | None) -> None:
        self.linked.append((refund_id, purchase))

    def insert_event(self, conn: Any, **kwargs: Any) -> ReceiptEvent:
        return ReceiptEvent(id=uuid4(), created_at=_NOW, **kwargs)


def _run(monkeypatch: pytest.MonkeyPatch, repo: _Repo) -> list[ReceiptEvent]:
    for name in (
        "find_refund_purchases",
        "lock_if_purchase",
        "was_unlinked",
        "set_refund_of",
        "insert_event",
    ):
        monkeypatch.setattr(refunds, name, getattr(repo, name))
    return refunds.link_by_rule(MagicMock(), _refund())


class TestLocking:
    def test_links_the_one_match_after_locking_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        purchase = uuid4()
        repo = _Repo([purchase], still_purchase=True)
        events = _run(monkeypatch, repo)
        assert repo.locked == [purchase]
        assert [p for _, p in repo.linked] == [purchase]
        assert [e.event_type for e in events] == ["refund_linked"]

    def test_a_match_that_stopped_being_a_purchase_is_not_linked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        purchase = uuid4()
        repo = _Repo([purchase], still_purchase=False)
        assert _run(monkeypatch, repo) == []
        assert repo.linked == []

    def test_several_matches_lock_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Locking candidates the rule won't link would add deadlock cycles
        # with concurrent edits of those purchases.
        repo = _Repo([uuid4(), uuid4()], still_purchase=True)
        assert _run(monkeypatch, repo) == []
        assert repo.locked == []
        assert repo.linked == []
