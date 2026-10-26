"""Re-extraction's run loop: input order, outages, failures and locks.

The repository calls are replaced by an in-memory fake, the LLM by mocks
with real integer usage, and the sources by fake adapters.
"""

from __future__ import annotations

import io
from contextlib import ExitStack, contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from receipt_index import reextract
from receipt_index.adapters.base import SourceUnavailableError
from receipt_index.adapters.imap import MessageRefusedError
from receipt_index.config import ImapSourceConfig
from receipt_index.extraction import EXTRACTION_PROMPT_VERSION
from receipt_index.models import (
    ExtractionResult,
    FieldConfidence,
    RawReceipt,
    Receipt,
    ReceiptMetadata,
)
from receipt_index.reextract import SourcePool, run_reextract

if TYPE_CHECKING:
    from collections.abc import Iterator

_NOW = datetime(2026, 1, 15, tzinfo=UTC)
_FC = {"vendor": 0.9, "amount": 0.9, "paid_amount": 0.4, "date": 0.9}


def _receipt(n: int = 1, **overrides: Any) -> Receipt:
    values: dict[str, Any] = {
        "id": UUID(int=n),
        "source_id": f"<r{n}@example.com>",
        "source_type": "imap",
        "source_name": "personal-email",
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("50.00"),
        "paid_amount": None,
        "currency": "USD",
        "receipt_date": date(2026, 1, n),
        "description": "Parking",
        "confidence": 0.9,
        "field_confidence": _FC,
        "manual_fields": [],
        "pdf_path": f"renditions/2026/01/r{n}.pdf",
        "email_subject": f"Receipt {n}",
        "email_sender": "r@example.com",
        "email_date": _NOW,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return Receipt.model_validate(values)


def _result(confidence: float = 0.95, paid: str | None = "30.00") -> ExtractionResult:
    return ExtractionResult(
        metadata=ReceiptMetadata(
            vendor="Acme Parking (U.S.), LLC",
            amount=Decimal("50.00"),
            paid_amount=None if paid is None else Decimal(paid),
            date=date(2026, 1, 1),
            description="Parking",
            confidence=confidence,
            field_confidence=FieldConfidence(
                vendor=0.9, amount=0.9, paid_amount=0.8, date=0.9
            ),
        ),
        input_tokens=1200,
        output_tokens=180,
        cache_read_tokens=0,
        requests=1,
    )


class FakeDb:
    """The repository functions reextract calls, over an in-memory table."""

    def __init__(self, receipts: list[Receipt]) -> None:
        self.receipts = {r.id: r for r in receipts}
        self.extractions: list[dict[str, Any]] = []
        self.updates: list[tuple[UUID, dict[str, Any], dict[str, float] | None]] = []
        self.locked: set[str] = set()
        # Applied between the extraction and the apply (a hand edit).
        self.edit_before_apply: dict[UUID, dict[str, Any]] = {}

    @contextmanager
    def item_lock(self, conn: Any, source_id: str) -> Iterator[bool]:
        yield source_id not in self.locked

    def lock_receipt(self, conn: Any, receipt_id: UUID) -> Receipt | None:
        receipt = self.receipts.get(receipt_id)
        edit = self.edit_before_apply.pop(receipt_id, None)
        if receipt is not None and edit:
            receipt = receipt.model_copy(update=edit)
            self.receipts[receipt_id] = receipt
        return receipt

    def get_extraction_history(
        self, conn: Any, receipt_id: UUID
    ) -> tuple[bool, set[str]]:
        rows = [e for e in self.extractions if e["receipt_id"] == receipt_id]
        ingested = any(e["origin"] == "ingest" for e in rows)
        return ingested, {f for e in rows for f in e["applied_fields"]}

    def update_receipt_fields(
        self,
        conn: Any,
        receipt_id: UUID,
        columns: dict[str, Any],
        field_confidence: dict[str, float] | None = None,
    ) -> None:
        self.updates.append((receipt_id, columns, field_confidence))

    def insert_extraction(self, conn: Any, **kwargs: Any) -> None:
        self.extractions.append(kwargs)


class FakeAdapter:
    """A source that serves items by ID, or fails."""

    source_type = "imap"

    def __init__(self, items: dict[str, Any]) -> None:
        self.items = items
        self.calls: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def fetch_by_id(self, source_id: str) -> RawReceipt | None:
        self.calls.append(source_id)
        item = self.items.get(source_id)
        if isinstance(item, Exception):
            raise item
        return item


def _raw(receipt: Receipt) -> RawReceipt:
    return RawReceipt(
        source_id=receipt.source_id,
        source_name=receipt.source_name,
        source_type="imap",
        date=_NOW,
        subject=receipt.email_subject or "",
        text_body="Total 50.00, paid 30.00",
    )


def _config(name: str = "personal-email") -> ImapSourceConfig:
    return ImapSourceConfig(
        name=name,
        host="mail.example.com",
        username="user@example.com",
        password="x",  # pragma: allowlist secret
    )


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    fake = FakeDb([])
    for name in (
        "item_lock",
        "lock_receipt",
        "get_extraction_history",
        "update_receipt_fields",
        "insert_extraction",
    ):
        monkeypatch.setattr(reextract, name, getattr(fake, name))
    return fake


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    mocks = {
        "source": MagicMock(return_value=_result()),
        "rendition": MagicMock(return_value=_result()),
    }
    monkeypatch.setattr(reextract, "extract_metadata", mocks["source"])
    monkeypatch.setattr(reextract, "extract_from_rendition", mocks["rendition"])
    return mocks


_AGENTS: dict[str, Any] = {
    "email": MagicMock(name="email"),
    "document": MagicMock(name="doc"),
}


def _store() -> MagicMock:
    store = MagicMock()
    store.open.side_effect = lambda ref: io.BytesIO(b"%PDF-" + ref.encode())
    return store


def _run(
    db: FakeDb,
    receipts: list[Receipt],
    *,
    adapter: FakeAdapter | None,
    configs: list[ImapSourceConfig] | None = None,
    store: MagicMock | None = None,
    overwrite: bool = False,
) -> tuple[reextract.ReextractResult, MagicMock]:
    db.receipts.update({r.id: r for r in receipts})
    store = store or _store()
    factory = MagicMock(return_value=adapter)
    with ExitStack() as stack:
        pool = (
            None
            if adapter is None
            else SourcePool(configs or [_config()], factory, stack)
        )
        result = run_reextract(
            MagicMock(),
            receipts,
            pool=pool,
            store=store,
            agents=_AGENTS,
            llm_model="claude-test",
            overwrite=overwrite,
        )
    return result, factory


class TestInputOrder:
    def test_source_first(self, db: FakeDb, llm: dict[str, MagicMock]) -> None:
        receipt = _receipt()
        adapter = FakeAdapter({receipt.source_id: _raw(receipt)})
        result, _ = _run(db, [receipt], adapter=adapter)

        (outcome,) = result.outcomes
        assert outcome.status == "applied"
        assert outcome.input_kind == "source"
        _, kwargs = llm["source"].call_args
        assert kwargs["agent"] is _AGENTS["email"]
        llm["rendition"].assert_not_called()
        (row,) = db.extractions
        assert row["origin"] == "reextract"
        assert row["input_kind"] == "source"
        assert row["prompt_version"] == EXTRACTION_PROMPT_VERSION
        assert row["llm_model"] == "claude-test"
        assert row["applied_fields"] == ["paid_amount", "field_confidence.paid_amount"]
        assert row["replaced"] == {
            "paid_amount": None,
            "field_confidence.paid_amount": 0.4,
        }
        assert (row["llm_input_tokens"], row["llm_output_tokens"]) == (1200, 180)

    def test_gone_item_falls_back_to_the_rendition(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt()
        store = _store()
        result, _ = _run(db, [receipt], adapter=FakeAdapter({}), store=store)

        assert result.outcomes[0].input_kind == "rendition"
        store.open.assert_called_once_with("local:renditions/2026/01/r1.pdf")
        (pdf,), kwargs = llm["rendition"].call_args
        assert pdf == b"%PDF-local:renditions/2026/01/r1.pdf"
        assert kwargs["subject"] == "Receipt 1"
        assert kwargs["sender"] == "r@example.com"
        assert kwargs["agent"] is _AGENTS["document"]
        assert db.extractions[0]["input_kind"] == "rendition"

    def test_refused_item_falls_back_to_the_rendition(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt()
        adapter = FakeAdapter({receipt.source_id: MessageRefusedError("NO corrupt")})
        result, _ = _run(db, [receipt], adapter=adapter)
        assert result.outcomes[0].input_kind == "rendition"
        assert result.outcomes[0].status == "applied"

    def test_rendition_mode_never_opens_a_source(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        result, factory = _run(db, [_receipt()], adapter=None)
        assert result.outcomes[0].input_kind == "rendition"
        factory.assert_not_called()
        llm["source"].assert_not_called()

    def test_unconfigured_source_tries_sources_of_its_type(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt(source_name="legacy-imap")
        adapter = FakeAdapter({receipt.source_id: _raw(receipt)})
        result, factory = _run(db, [receipt], adapter=adapter)
        assert result.outcomes[0].input_kind == "source"
        assert factory.call_args[0][0].name == "personal-email"

    def test_no_source_of_its_type_uses_the_rendition(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt(source_name="old-drive", source_type="gdrive")
        result, factory = _run(db, [receipt], adapter=FakeAdapter({}))
        assert result.outcomes[0].input_kind == "rendition"
        factory.assert_not_called()


class TestOutages:
    def test_unavailable_source_writes_nothing_and_isnt_retried_this_run(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipts = [_receipt(1), _receipt(2), _receipt(3)]
        adapter = FakeAdapter(
            {r.source_id: SourceUnavailableError("timed out") for r in receipts}
        )
        result, _ = _run(db, receipts, adapter=adapter)

        assert [o.status for o in result.outcomes] == ["unavailable"] * 3
        assert adapter.calls == ["<r1@example.com>"]  # contacted once
        assert db.extractions == []  # retried by the next run
        llm["source"].assert_not_called()
        llm["rendition"].assert_not_called()
        assert not result.ok

    def test_failure_is_recorded_and_the_run_moves_on(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        llm["rendition"].side_effect = [RuntimeError("overloaded"), _result()]
        result, _ = _run(db, [_receipt(1), _receipt(2)], adapter=None)

        assert [o.status for o in result.outcomes] == ["failed", "applied"]
        failed = db.extractions[0]
        assert failed["output"] == {"error": "RuntimeError: overloaded"}
        assert failed["applied_fields"] == []
        assert failed["llm_model"] == "claude-test"
        assert not result.ok

    def test_stops_after_failures_in_a_row(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        llm["rendition"].side_effect = RuntimeError("credit balance too low")
        receipts = [_receipt(n) for n in range(1, 9)]
        result, _ = _run(db, receipts, adapter=None)

        assert len(result.outcomes) == 5
        assert result.stopped is not None
        assert "5 failures in a row" in result.stopped
        assert llm["rendition"].call_count == 5

    def test_missing_rendition_is_a_failure(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        store = MagicMock()
        store.open.side_effect = FileNotFoundError("no such file")
        result, _ = _run(db, [_receipt()], adapter=None, store=store)
        assert result.outcomes[0].status == "failed"
        assert "FileNotFoundError" in (result.outcomes[0].error or "")
        llm["rendition"].assert_not_called()

    def test_locked_receipt_is_skipped(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt()
        db.locked.add(receipt.source_id)
        result, _ = _run(db, [receipt], adapter=None)
        assert result.outcomes[0].status == "locked"
        assert db.extractions == []
        llm["rendition"].assert_not_called()


class TestApplying:
    def test_gated_records_and_applies_nothing(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        llm["rendition"].return_value = _result(confidence=0.3)
        result, _ = _run(db, [_receipt()], adapter=None)
        assert result.outcomes[0].status == "gated"
        assert db.updates == []
        (row,) = db.extractions
        assert row["applied_fields"] == []
        assert row["output"]["confidence"] == 0.3
        assert not result.ok

    def test_report_carries_the_extracted_paid_amount_when_not_applied(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt(paid_amount=Decimal("25.00"))
        result, _ = _run(db, [receipt], adapter=None)
        outcome = result.outcomes[0]
        assert outcome.status == "unchanged"
        assert outcome.extracted_paid_amount == Decimal("30.00")
        assert outcome.paid_amount_confidence == 0.8
        assert outcome.to_json()["extracted_paid_amount"] == "30.00"
        assert result.ok

    def test_a_hand_edit_during_the_llm_call_is_respected(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt()
        db.edit_before_apply[receipt.id] = {
            "paid_amount": Decimal("29.00"),
            "manual_fields": ["paid_amount"],
        }
        result, _ = _run(db, [receipt], adapter=None)
        assert result.outcomes[0].status == "unchanged"
        assert db.updates == []

    def test_writes_columns_and_merged_confidence(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt()
        _run(db, [receipt], adapter=None)
        ((receipt_id, columns, fc),) = db.updates
        assert receipt_id == receipt.id
        assert columns == {"paid_amount": Decimal("30.00")}
        assert fc == {**_FC, "paid_amount": 0.8}

    def test_a_second_run_changes_nothing(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt()
        _run(db, [receipt], adapter=None)
        db.receipts[receipt.id] = receipt.model_copy(
            update={
                "paid_amount": Decimal("30.00"),
                "field_confidence": {**_FC, "paid_amount": 0.8},
            }
        )
        result, _ = _run(db, [db.receipts[receipt.id]], adapter=None)
        assert result.outcomes[0].status == "unchanged"
        assert len(db.updates) == 1


class TestApplyFailures:
    def test_a_failed_write_rolls_back_and_records_an_error_row(
        self, db: FakeDb, llm: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_: Any, **__: Any) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(reextract, "update_receipt_fields", broken)
        conn = MagicMock()
        receipt = _receipt()
        db.receipts[receipt.id] = receipt
        outcome = reextract.reextract_receipt(
            conn,
            receipt,
            pool=None,
            store=_store(),
            agents=_AGENTS,
            llm_model="claude-test",
        )

        assert outcome.status == "failed"
        assert outcome.error == "RuntimeError: disk full"
        conn.rollback.assert_called()  # the half-applied transaction is dropped
        (row,) = db.extractions
        assert row["output"] == {"error": "RuntimeError: disk full"}
        assert row["applied_fields"] == []
        # The LLM call was paid for: its tokens are still recorded.
        assert (row["llm_input_tokens"], row["llm_output_tokens"]) == (1200, 180)

    def test_a_receipt_deleted_during_the_run_writes_nothing(
        self, db: FakeDb, llm: dict[str, MagicMock]
    ) -> None:
        receipt = _receipt()
        conn = MagicMock()
        outcome = reextract.reextract_receipt(
            conn,
            receipt,  # selected, but no longer in the table
            pool=None,
            store=_store(),
            agents=_AGENTS,
            llm_model="claude-test",
        )
        assert outcome.status == "failed"
        assert outcome.error == "receipt was deleted"
        assert db.extractions == []
        assert db.updates == []
        conn.rollback.assert_called()
