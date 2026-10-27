"""Re-extraction against the devtest Postgres (data-model design, §3).

Receipts are seeded directly, with renditions in a temporary store, and the
LLM is a mock that answers by subject. These tests prove the selection SQL,
the writes and the history rows, not a model's accuracy. Vendors and amounts
are placeholders.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.config import ImapSourceConfig
from receipt_index.extraction import EXTRACTION_PROMPT_VERSION
from receipt_index.models import FieldConfidence, ReceiptMetadata
from receipt_index.pipeline import run_ingest
from receipt_index.reextract import SourcePool, run_reextract
from receipt_index.repository import (
    get_receipt_by_id,
    insert_extraction,
    insert_receipt,
    search_receipts,
    select_for_reextract,
)
from receipt_index.store import LocalEvidenceStore, pdf_path_for, store_rendition

from .conftest import GREENMAIL_IMAP_PORT, seed_email

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg

    from receipt_index.models import Receipt

    Conn = psycopg.Connection[dict[str, Any]]

pytestmark = pytest.mark.integration

_MODEL = "claude-test"
_FC = {"vendor": 0.9, "amount": 0.9, "paid_amount": 0.4, "date": 0.9}


def _meta(subject: str, **overrides: Any) -> ReceiptMetadata:
    values: dict[str, Any] = {
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("50.00"),
        "paid_amount": Decimal("30.00"),
        "date": date(2026, 1, 15),
        "description": f"Parking ({subject})",
        "confidence": 0.95,
        "field_confidence": FieldConfidence(
            vendor=0.95, amount=0.9, paid_amount=0.85, date=0.9
        ),
    }
    values.update(overrides)
    return ReceiptMetadata.model_validate(values)


class Llm:
    """A mock agent answering by the subject line; counts its calls."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[str] = []
        self.agent = MagicMock()
        self.agent.run_sync.side_effect = self._run

    def _run(self, message: Any) -> MagicMock:
        text = message if isinstance(message, str) else message[0]
        subject = text.split("Subject: ", 1)[1].split("\n", 1)[0]
        self.calls.append(subject)
        answer = self.answers.get(subject, _meta(subject))
        if isinstance(answer, Exception):
            raise answer
        usage = MagicMock()
        usage.input_tokens = 1500
        usage.output_tokens = 200
        usage.cache_read_tokens = 0
        usage.requests = 1
        result = MagicMock()
        result.output = answer
        result.usage = usage
        return result

    @property
    def agents(self) -> dict[str, Any]:
        return {"email": self.agent, "document": self.agent}


def _seed(
    conn: Conn,
    store: LocalEvidenceStore,
    subject: str,
    *,
    amount: str = "30.00",
    paid: str | None = None,
    field_confidence: dict[str, float] | None = None,
    receipt_date: date = date(2026, 1, 15),
    manual: list[str] | None = None,
) -> Receipt:
    stored = store_rendition(
        store,
        b"%PDF-1.4 not really a pdf " + subject.encode(),
        receipt_date=receipt_date,
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal(amount),
    )
    receipt = insert_receipt(
        conn,
        source_id=f"<{uuid.uuid4().hex}@example.com>",
        source_type="imap",
        source_name="personal-email",
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal(amount),
        paid_amount=None if paid is None else Decimal(paid),
        currency="USD",
        receipt_date=receipt_date,
        description=None,
        confidence=0.9,
        field_confidence=field_confidence or {},
        pdf_path=pdf_path_for(stored.ref),
        email_subject=subject,
        email_sender="receipts@example.com",
        email_date=None,
    )
    if manual:
        conn.execute(
            "UPDATE receipt.receipts SET manual_fields = %s WHERE id = %s",
            (manual, receipt.id),
        )
        conn.commit()
    return receipt


def _history(conn: Conn, receipt_id: Any) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM receipt.extractions WHERE receipt_id = %s ORDER BY created_at",
        (receipt_id,),
    ).fetchall()


def _select(conn: Conn, **kw: Any) -> list[Receipt]:
    rows = select_for_reextract(
        conn, prompt_version=EXTRACTION_PROMPT_VERSION, llm_model=_MODEL, **kw
    )
    conn.commit()
    return rows


def _run(
    conn: Conn, store: LocalEvidenceStore, llm: Llm, receipts: list[Receipt], **kw: Any
) -> Any:
    return run_reextract(
        conn,
        receipts,
        pool=None,
        store=store,
        agents=llm.agents,
        llm_model=_MODEL,
        **kw,
    )


def _good_row(conn: Conn, receipt_id: Any, *, version: int, model: str) -> None:
    insert_extraction(
        conn,
        receipt_id=receipt_id,
        origin="ingest",
        input_kind="source",
        prompt_version=version,
        llm_model=model,
        output={"confidence": 0.9},
        applied_fields=[],
        replaced={},
        llm_input_tokens=1000,
        llm_output_tokens=100,
        llm_cache_read_tokens=0,
        llm_requests=1,
    )
    conn.commit()


class TestFirstRun:
    def test_fills_paid_amount_replaces_the_old_total_and_records_it(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        old = _seed(pg_conn, store, "Old prompt")  # amount 30.00 was the payment
        llm = Llm()

        result = _run(pg_conn, store, llm, _select(pg_conn, ids=[old.id]))

        assert [o.status for o in result.outcomes] == ["applied"]
        after = get_receipt_by_id(pg_conn, old.id)
        assert after is not None
        assert after.paid_amount == Decimal("30.00")
        assert after.amount == Decimal("50.00")  # now the total
        assert after.field_confidence["paid_amount"] == pytest.approx(0.85)
        (row,) = _history(pg_conn, old.id)
        assert row["origin"] == "reextract"
        assert row["input_kind"] == "rendition"
        assert row["prompt_version"] == EXTRACTION_PROMPT_VERSION
        assert row["llm_model"] == _MODEL
        assert "amount" in row["applied_fields"]
        assert "paid_amount" in row["applied_fields"]
        assert row["replaced"]["amount"] == "30.00"
        assert row["replaced"]["paid_amount"] is None
        assert row["output"]["paid_amount"] == "30.00"
        assert row["llm_input_tokens"] == 1500

    def test_a_second_run_selects_nothing_and_calls_no_llm(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        receipt = _seed(pg_conn, store, "Twice")
        llm = Llm()
        _run(pg_conn, store, llm, _select(pg_conn, ids=[receipt.id]))
        assert _select(pg_conn, ids=[receipt.id]) == []
        assert llm.calls == ["Twice"]

    def test_a_hand_edited_field_survives_overwrite(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        receipt = _seed(
            pg_conn,
            store,
            "Edited",
            paid="28.00",
            field_confidence=_FC,
            manual=["paid_amount"],
        )
        result = _run(
            pg_conn,
            store,
            Llm(),
            _select(pg_conn, ids=[receipt.id]),
            overwrite=True,
        )
        after = get_receipt_by_id(pg_conn, receipt.id)
        assert after is not None
        assert after.paid_amount == Decimal("28.00")
        assert after.field_confidence["paid_amount"] == pytest.approx(0.4)
        assert "paid_amount" not in result.outcomes[0].changes

    def test_receipts_from_the_paid_amount_prompt_keep_their_amount(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        receipt = _seed(
            pg_conn, store, "Ticket one", paid="30.00", field_confidence=_FC
        )
        _run(pg_conn, store, Llm(), _select(pg_conn, ids=[receipt.id]))
        after = get_receipt_by_id(pg_conn, receipt.id)
        assert after is not None
        assert after.amount == Decimal("30.00")


class TestOverlappingRuns:
    def test_a_second_run_with_a_stale_selection_doesnt_pay_again(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        receipt = _seed(pg_conn, store, "Overlap")
        selected_by_b = _select(pg_conn, ids=[receipt.id])  # B selects first
        llm = Llm()
        _run(pg_conn, store, llm, _select(pg_conn, ids=[receipt.id]))  # A runs
        result = _run(pg_conn, store, llm, selected_by_b)  # then B
        assert result.outcomes[0].status == "current"
        assert llm.calls == ["Overlap"]  # paid once
        assert len(_history(pg_conn, receipt.id)) == 1


class TestAfterAGatedRun:
    def test_the_old_prompt_amount_is_still_replaced(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        old = _seed(pg_conn, store, "Gated first")  # amount 30.00, the payment
        gated = Llm({"Gated first": _meta("Gated first", confidence=0.3)})
        first = _run(pg_conn, store, gated, _select(pg_conn, ids=[old.id]))
        assert first.outcomes[0].status == "gated"

        # A gated run is a real result: the receipt is current until --force.
        assert _select(pg_conn, ids=[old.id]) == []
        _run(
            pg_conn,
            store,
            Llm(),
            _select(pg_conn, ids=[old.id], force=True),
            force=True,
        )

        after = get_receipt_by_id(pg_conn, old.id)
        assert after is not None
        assert after.amount == Decimal("50.00")  # replaced by the total
        assert after.paid_amount == Decimal("30.00")
        second = _history(pg_conn, old.id)[1]
        assert second["replaced"]["amount"] == "30.00"

    def test_a_legacy_receipt_stays_overwritable_after_a_run(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        old = _seed(pg_conn, store, "Legacy")
        _run(pg_conn, store, Llm(), _select(pg_conn, ids=[old.id]))
        corrected = Llm({"Legacy": _meta("Legacy", vendor="Acme Parking")})
        _run(
            pg_conn,
            store,
            corrected,
            _select(pg_conn, ids=[old.id], force=True),
            overwrite=True,
            force=True,
        )
        after = get_receipt_by_id(pg_conn, old.id)
        assert after is not None
        assert after.vendor == "Acme Parking"


class TestSelection:
    def test_current_version_and_model_are_skipped(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        fresh = _seed(pg_conn, store, "Fresh")
        current = _seed(pg_conn, store, "Current")
        _good_row(pg_conn, current.id, version=EXTRACTION_PROMPT_VERSION, model=_MODEL)
        old_model = _seed(pg_conn, store, "Old model")
        _good_row(
            pg_conn, old_model.id, version=EXTRACTION_PROMPT_VERSION, model="older"
        )
        old_version = _seed(pg_conn, store, "Old version")
        _good_row(pg_conn, old_version.id, version=1, model=_MODEL)

        selected = {r.id for r in _select(pg_conn)}
        assert selected == {fresh.id, old_model.id, old_version.id}
        assert current.id in {r.id for r in _select(pg_conn, force=True)}

    def test_a_failed_run_does_not_make_a_receipt_current(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        receipt = _seed(pg_conn, store, "Flaky")
        llm = Llm({"Flaky": RuntimeError("overloaded")})
        result = _run(pg_conn, store, llm, _select(pg_conn, ids=[receipt.id]))

        assert result.outcomes[0].status == "failed"
        (row,) = _history(pg_conn, receipt.id)
        assert row["output"] == {"error": "RuntimeError: overloaded"}
        assert row["prompt_version"] == EXTRACTION_PROMPT_VERSION
        assert [r.id for r in _select(pg_conn, ids=[receipt.id])] == [receipt.id]

    def test_model_not(self, pg_conn: Conn, tmp_path: Path) -> None:
        store = LocalEvidenceStore(tmp_path)
        none_yet = _seed(pg_conn, store, "None yet")
        by_old = _seed(pg_conn, store, "By old")
        _good_row(pg_conn, by_old.id, version=1, model="older")
        by_new = _seed(pg_conn, store, "By new")
        _good_row(pg_conn, by_new.id, version=1, model="newer")

        selected = {r.id for r in _select(pg_conn, model_not="newer", force=True)}
        assert selected == {none_yet.id, by_old.id}

    def test_confidence_below(self, pg_conn: Conn, tmp_path: Path) -> None:
        store = LocalEvidenceStore(tmp_path)
        low = _seed(pg_conn, store, "Low", field_confidence={**_FC, "paid_amount": 0.3})
        high = _seed(
            pg_conn, store, "High", field_confidence={**_FC, "paid_amount": 0.9}
        )
        missing = _seed(pg_conn, store, "Missing")

        selected = {
            r.id
            for r in _select(pg_conn, confidence_below=("paid_amount", 0.5), force=True)
        }
        assert selected == {low.id, missing.id}
        assert high.id not in selected
        overall = _select(pg_conn, confidence_below=(None, 0.95), force=True)
        assert {r.id for r in overall} == {low.id, high.id, missing.id}  # all 0.9

    def test_date_range_and_limit(self, pg_conn: Conn, tmp_path: Path) -> None:
        store = LocalEvidenceStore(tmp_path)
        jan = _seed(pg_conn, store, "Jan", receipt_date=date(2026, 1, 10))
        feb = _seed(pg_conn, store, "Feb", receipt_date=date(2026, 2, 10))
        _seed(pg_conn, store, "Mar", receipt_date=date(2026, 3, 10))
        rows = _select(pg_conn, date_from=date(2026, 1, 1), date_to=date(2026, 2, 28))
        assert [r.id for r in rows] == [jan.id, feb.id]
        assert [r.id for r in _select(pg_conn, limit=1)] == [jan.id]


class TestNeedsReview:
    def test_lists_missing_paid_and_gated_lowest_confidence_first(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        store = LocalEvidenceStore(tmp_path)
        missing_low = _seed(
            pg_conn, store, "Missing low", field_confidence={**_FC, "paid_amount": 0.2}
        )
        missing_none = _seed(pg_conn, store, "Missing none")
        done = _seed(pg_conn, store, "Done", paid="30.00", field_confidence=_FC)
        gated = _seed(pg_conn, store, "Gated", paid="30.00", field_confidence=_FC)
        llm = Llm({"Gated": _meta("Gated", confidence=0.3)})
        _run(pg_conn, store, llm, _select(pg_conn, ids=[gated.id, done.id]))

        listed = [r.id for r in search_receipts(pg_conn, needs_review=True)]
        # Unchanged after a run that applied nothing because nothing was
        # empty: not listed. Gated: listed.
        assert done.id not in listed
        assert listed[:2] == [missing_none.id, missing_low.id]
        assert gated.id in listed


@pytest.mark.usefixtures("greenmail_available")
class TestThroughGreenMail:
    def _config(self) -> ImapSourceConfig:
        return ImapSourceConfig(
            name="greenmail-reextract",
            host="localhost",
            username=f"reextract-{uuid.uuid4().hex[:8]}@localhost",
            password="any",  # pragma: allowlist secret
            port=GREENMAIL_IMAP_PORT,
            folder="INBOX",
            use_ssl=False,
        )

    def test_ingest_records_its_extraction_and_reextract_reads_the_source(
        self, pg_conn: Conn, tmp_path: Path
    ) -> None:
        config = self._config()
        store = LocalEvidenceStore(tmp_path)
        seed_email(
            to_addr=config.username,
            subject="Parking receipt",
            text_body="Acme Parking (U.S.), LLC. Total 50.00, card 30.00",
            message_id=f"<{uuid.uuid4().hex}@example.com>",
        )
        llm = Llm()
        run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(config),
            store=store,
            source_name=config.name,
            source_type="imap",
            agent=llm.agent,
            llm_model=_MODEL,
        )
        (receipt,) = search_receipts(pg_conn, source_name=config.name)
        (ingest_row,) = _history(pg_conn, receipt.id)
        assert ingest_row["origin"] == "ingest"
        assert ingest_row["input_kind"] == "source"
        assert ingest_row["llm_model"] == _MODEL
        assert "receipt_document" in ingest_row["output"]

        # Current after ingest: only --force selects it again.
        assert _select(pg_conn, ids=[receipt.id]) == []
        with ExitStack() as stack:
            pool = SourcePool([config], ImapAdapter, stack)
            result = run_reextract(
                pg_conn,
                _select(pg_conn, ids=[receipt.id], force=True),
                pool=pool,
                store=store,
                agents=llm.agents,
                llm_model=_MODEL,
                force=True,
            )
        assert result.outcomes[0].input_kind == "source"
        assert result.outcomes[0].status == "unchanged"
        assert [r["origin"] for r in _history(pg_conn, receipt.id)] == [
            "ingest",
            "reextract",
        ]
