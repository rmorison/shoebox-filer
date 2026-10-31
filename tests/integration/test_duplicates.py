"""Duplicate detection, link, unlink and the scan (evidence-store design, §8).

Items run through ``process_item`` with the extraction and the renderer
stubbed: the rules see what the extraction would report, and the database is
the real devtest schema. Every value here is synthetic.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import psycopg
import pytest

from receipt_index import duplicates, pipeline
from receipt_index.duplicates import Actor
from receipt_index.models import (
    Attachment,
    ExtractionResult,
    FieldConfidence,
    Provenance,
    RawReceipt,
    ReceiptMetadata,
)
from receipt_index.refunds import link_by_rule
from receipt_index.renderer import Rendition
from receipt_index.repository import (
    count_receipts,
    get_receipt_by_id,
    search_receipts,
    select_for_reextract,
    with_provenance,
)
from receipt_index.store import LocalEvidenceStore
from tests.pdfs import make_pdf

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from receipt_index.models import Receipt

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]

VENDOR = "Acme Parking (U.S.), LLC"
PERSON = Actor("operator", "person", "test")
_WHEN = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _meta(**kw: Any) -> ReceiptMetadata:
    values: dict[str, Any] = {
        "vendor": VENDOR,
        "amount": Decimal("12.00"),
        "paid_amount": Decimal("12.00"),
        "date": date(2026, 10, 14),
        "confidence": 0.95,
        "field_confidence": FieldConfidence(
            vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9
        ),
        "receipt_document": "body",
    }
    values.update(kw)
    return ReceiptMetadata(**values)


def _email(pdf: bytes | None = None, *, name: str = "receipt.pdf") -> RawReceipt:
    mid = f"<{uuid.uuid4().hex}@example.com>"
    attachments = (
        [Attachment(name, "application/pdf", pdf, mime_filename=name)]
        if pdf is not None
        else []
    )
    return RawReceipt(
        source_id=mid,
        source_name="mail",
        source_type="imap",
        date=_WHEN,
        subject="Your receipt",
        sender="billing@example.com",
        text_body="Thanks for parking.",
        attachments=attachments,
        # Each email's bytes differ: every server adds its own headers.
        raw_bytes=f"Message-ID: {mid}\r\n\r\nbody".encode(),
    )


def _drive(pdf: bytes) -> RawReceipt:
    fid = f"drive-{uuid.uuid4().hex[:12]}"
    return RawReceipt(
        source_id=fid,
        source_name="drive",
        source_type="gdrive",
        date=_WHEN,
        file_name="scan.pdf",
        file_content=pdf,
        file_content_type="application/pdf",
        raw_bytes=pdf,
    )


class _Ingest:
    """Runs items through process_item, counting the LLM calls."""

    def __init__(
        self, conn: Conn, store: LocalEvidenceStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.conn, self.store = conn, store
        self.calls = 0
        self.meta = _meta()
        monkeypatch.setattr(pipeline, "extract_metadata", self._extract)
        monkeypatch.setattr(
            pipeline,
            "render_rendition",
            lambda raw: Rendition(pdf=make_pdf(f"rendition {raw.source_id}")),
        )

    def _extract(self, raw: RawReceipt, **_: Any) -> ExtractionResult:
        self.calls += 1
        return ExtractionResult(
            metadata=self.meta,
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=0,
            requests=1,
        )

    def __call__(self, raw: RawReceipt, **meta: Any) -> Receipt:
        self.meta = _meta(**meta)
        outcome = pipeline.process_item(
            self.conn,
            raw,
            store=self.store,
            source_name=raw.source_name,
            source_type=raw.source_type,
            llm_model="test-model",
        )
        assert outcome.status == "success", outcome
        assert outcome.receipt is not None
        return outcome.receipt


@pytest.fixture
def ingest(pg_conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Ingest:
    return _Ingest(pg_conn, LocalEvidenceStore(tmp_path), monkeypatch)


def _all(conn: Conn, sql: str, *args: Any) -> list[dict[str, Any]]:
    rows = conn.execute(sql, args).fetchall()
    conn.rollback()
    return rows


def _live(conn: Conn) -> list[Receipt]:
    rows = search_receipts(conn)
    conn.rollback()
    return rows


def _total(conn: Conn) -> Decimal:
    return sum((r.signed_amount for r in _live(conn)), Decimal(0))


def _candidates(conn: Conn) -> list[dict[str, Any]]:
    return _all(conn, "SELECT * FROM receipt.duplicate_candidates ORDER BY created_at")


def _prov(conn: Conn, source_id: str) -> Provenance:
    (row,) = _all(
        conn, "SELECT * FROM receipt.provenance WHERE source_id = %s", source_id
    )
    return Provenance.model_validate(row)


def _history(conn: Conn) -> list[tuple[str, str]]:
    rows = _all(
        conn,
        "SELECT action, match_method FROM receipt.provenance_link_history"
        " ORDER BY created_at, id",
    )
    return [(r["action"], r["match_method"]) for r in rows]


# ---------------------------------------------------------------------------
# Rules at ingest
# ---------------------------------------------------------------------------


class TestRule1:
    def test_the_same_drive_pdf_twice_links_with_no_llm_call(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        pdf = make_pdf("Acme Parking receipt 12.00")
        first = ingest(_drive(pdf))
        second_raw = _drive(pdf)
        joined = ingest(second_raw)

        assert joined.id == first.id
        assert ingest.calls == 1
        assert [r.id for r in _live(pg_conn)] == [first.id]
        prov = _prov(pg_conn, second_raw.source_id)
        assert (prov.receipt_id, prov.is_primary, prov.home_receipt_id) == (
            first.id,
            False,
            None,
        )
        assert prov.match_method == "original_hash"
        assert _history(pg_conn) == [("link", "original_hash")]
        (log,) = _all(
            pg_conn,
            "SELECT status, receipt_id FROM receipt.ingest_log WHERE source_id = %s",
            second_raw.source_id,
        )
        assert log == {"status": "success", "receipt_id": first.id}


class TestRule2:
    def test_an_email_and_a_drive_pdf_of_one_receipt_link_and_count_once(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        # The issue's acceptance: linked, counted once, and unlinking
        # restores both.
        pdf = make_pdf("Acme Parking invoice 12.00")
        email = ingest(_email(pdf), receipt_document="receipt.pdf")
        drive_raw = _drive(pdf)
        joined = ingest(drive_raw)

        assert joined.id == email.id
        assert _prov(pg_conn, drive_raw.source_id).match_method == "document_hash"
        assert _total(pg_conn) == Decimal("12.00")

        prov = _prov(pg_conn, drive_raw.source_id)
        assert duplicates.unlink_home(pg_conn, prov.id, actor=PERSON) is None
        pg_conn.rollback()
        own = pipeline.unlink_to_own_receipt(
            pg_conn, prov, drive_raw, store=ingest.store, llm_model="m", actor=PERSON
        )
        assert own.id != email.id
        assert _total(pg_conn) == Decimal("24.00")
        moved = _prov(pg_conn, drive_raw.source_id)
        assert (moved.receipt_id, moved.is_primary, moved.home_receipt_id) == (
            own.id,
            True,
            own.id,
        )

    def test_a_drive_pdf_first_then_the_email_links_too(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        pdf = make_pdf("Acme Parking invoice, drive first")
        drive = ingest(_drive(pdf))
        joined = ingest(_email(pdf), receipt_document="receipt.pdf")
        assert joined.id == drive.id

    def test_an_ambiguous_attachment_never_links(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        pdf = make_pdf("terms and conditions")
        ingest(_email(pdf), receipt_document=None, vendor="Vendor A")
        ingest(_email(pdf), receipt_document=None, vendor="Vendor B")
        assert len(_live(pg_conn)) == 2
        assert _candidates(pg_conn) == []

    def test_a_refund_re_attaching_its_invoice_is_flagged_not_linked(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        pdf = make_pdf("invoice 12.00")
        ingest(_email(pdf), receipt_document="receipt.pdf", order_number="A-1")
        ingest(
            _email(pdf),
            receipt_document="receipt.pdf",
            kind="refund",
            order_number="A-1",
        )
        assert len(_live(pg_conn)) == 2
        (pair,) = _candidates(pg_conn)
        assert pair["reason"] == "document_hash_conflict"

    def test_the_guard_flags_a_different_amount(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        pdf = make_pdf("generic invoice template")
        ingest(_email(pdf), receipt_document="receipt.pdf")
        ingest(
            _email(pdf),
            receipt_document="receipt.pdf",
            amount=Decimal("30.00"),
            paid_amount=Decimal("30.00"),
        )
        assert len(_live(pg_conn)) == 2
        assert [c["reason"] for c in _candidates(pg_conn)] == ["document_hash_conflict"]


class TestRule3:
    def test_a_forward_with_the_same_order_links(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        first = ingest(_email(), order_number="ORD-7")
        joined = ingest(_email(), order_number="ORD-7")
        assert joined.id == first.id
        assert _history(pg_conn) == [("link", "order_number")]

    def test_a_different_date_within_the_window_is_flagged(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        ingest(_email(), order_number="ORD-8")
        ingest(_email(), order_number="ORD-8", date=date(2026, 10, 16))
        assert len(_live(pg_conn)) == 2
        assert [c["reason"] for c in _candidates(pg_conn)] == ["order_number_conflict"]

    def test_a_monthly_charge_outside_the_window_is_not_flagged(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        ingest(_email(), order_number="ACCT-1")
        ingest(_email(), order_number="ACCT-1", date=date(2026, 11, 14))
        assert len(_live(pg_conn)) == 2
        assert _candidates(pg_conn) == []

    @pytest.mark.parametrize("paid", [None, Decimal("0.00")])
    def test_no_paid_amount_never_links_by_order(
        self, ingest: _Ingest, pg_conn: Conn, paid: Decimal | None
    ) -> None:
        ingest(_email(), order_number="ORD-9", paid_amount=paid)
        ingest(_email(), order_number="ORD-9", paid_amount=paid)
        assert len(_live(pg_conn)) == 2

    def test_an_unpaid_invoice_and_its_receipt_are_neither_linked_nor_flagged(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        # The operator's case: an invoice (nothing charged) and the separate
        # payment receipt, same vendor, amount and invoice number.
        ingest(
            _email(),
            order_number="INV-100",
            amount=Decimal("100.00"),
            paid_amount=Decimal("0.00"),
        )
        ingest(
            _email(),
            order_number="INV-100",
            amount=Decimal("100.00"),
            paid_amount=Decimal("100.00"),
        )
        assert len(_live(pg_conn)) == 2
        assert _candidates(pg_conn) == []
        assert _total(pg_conn) == Decimal("100.00")

    def test_a_paid_invoice_and_its_receipt_on_other_dates_are_flagged(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        ingest(
            _email(),
            order_number="INV-200",
            amount=Decimal("100.00"),
            paid_amount=Decimal("100.00"),
        )
        ingest(
            _email(),
            order_number="INV-200",
            amount=Decimal("100.00"),
            paid_amount=Decimal("100.00"),
            date=date(2026, 10, 16),
        )
        assert len(_live(pg_conn)) == 2
        assert [c["reason"] for c in _candidates(pg_conn)] == ["order_number_conflict"]

    def test_a_refund_with_its_purchase_order_and_amount_is_not_linked(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        ingest(_email(), order_number="ORD-10")
        ingest(_email(), order_number="ORD-10", kind="refund")
        assert len(_live(pg_conn)) == 2
        assert _candidates(pg_conn) == []


class TestRule4:
    def test_two_identical_parking_charges_are_flagged_not_linked(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        ingest(_email())
        ingest(_email())
        assert len(_live(pg_conn)) == 2
        assert [c["reason"] for c in _candidates(pg_conn)] == ["metadata"]
        assert _total(pg_conn) == Decimal("24.00")

    def test_vendor_case_and_spacing_are_normalized(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        ingest(_email())
        ingest(_email(), vendor="  acme parking   (u.s.),  llc ")
        assert [c["reason"] for c in _candidates(pg_conn)] == ["metadata"]

    def test_another_currency_is_not_flagged(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        ingest(_email())
        ingest(_email(), currency="EUR")
        assert _candidates(pg_conn) == []

    def test_four_days_apart_is_not_flagged(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        ingest(_email())
        ingest(_email(), date=date(2026, 10, 18))
        assert _candidates(pg_conn) == []

    def test_the_pair_is_newer_then_older(self, ingest: _Ingest, pg_conn: Conn) -> None:
        a, b = _email(), _email()
        ingest(a)
        ingest(b)
        (pair,) = _candidates(pg_conn)
        assert pair["provenance_id"] == _prov(pg_conn, b.source_id).id
        assert pair["candidate_of_provenance_id"] == _prov(pg_conn, a.source_id).id

    def test_output_reports_the_open_pair(self, ingest: _Ingest, pg_conn: Conn) -> None:
        a = ingest(_email())
        b = ingest(_email())
        out = with_provenance(pg_conn, [a, b])
        pg_conn.rollback()
        assert out[0].suspected_duplicate_of == [b.id]
        assert out[1].suspected_duplicate_of == [a.id]
        assert [p.is_primary for p in out[0].provenance] == [True]


# ---------------------------------------------------------------------------
# Link, merge and unlink
# ---------------------------------------------------------------------------


def _two(ingest: _Ingest) -> tuple[Receipt, Receipt]:
    """Two receipts no rule relates (different vendors)."""
    return ingest(_email(), vendor="First Vendor"), ingest(
        _email(), vendor="Second Vendor"
    )


def _only_prov(conn: Conn, receipt_id: uuid.UUID) -> uuid.UUID:
    (row,) = _all(
        conn, "SELECT id FROM receipt.provenance WHERE receipt_id = %s", receipt_id
    )
    rid: uuid.UUID = row["id"]
    return rid


def _receipt(conn: Conn, receipt_id: uuid.UUID) -> Receipt:
    receipt = get_receipt_by_id(conn, receipt_id)
    conn.rollback()
    assert receipt is not None
    return receipt


class TestLinkAndUnlink:
    def test_round_trip_restores_the_merged_receipt_exactly(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        a, b = _two(ingest)
        b_prov = _only_prov(pg_conn, b.id)
        before = _all(pg_conn, "SELECT * FROM receipt.receipts WHERE id = %s", b.id)
        extractions = _all(
            pg_conn, "SELECT id FROM receipt.extractions WHERE receipt_id = %s", b.id
        )

        duplicates.link(pg_conn, b_prov, a.id, actor=PERSON)
        pg_conn.commit()
        merged = _receipt(pg_conn, b.id)
        assert merged.merged_into_id == a.id
        assert merged.merged_at is not None
        assert [r.id for r in _live(pg_conn)] == [a.id]
        assert (
            _all(
                pg_conn,
                "SELECT id FROM receipt.extractions WHERE receipt_id = %s",
                b.id,
            )
            == extractions
        )
        assert _all(
            pg_conn,
            "SELECT 1 AS x FROM receipt.file_locations WHERE receipt_id = %s",
            b.id,
        ) == [{"x": 1}]

        result = duplicates.unlink_home(pg_conn, b_prov, actor=PERSON)
        pg_conn.commit()
        assert result == duplicates.UnlinkResult(b.id, restored=True)
        after = _all(pg_conn, "SELECT * FROM receipt.receipts WHERE id = %s", b.id)
        # Every field as it was; only the trigger's updated_at moves.
        assert [{**r, "updated_at": None} for r in after] == [
            {**r, "updated_at": None} for r in before
        ]
        prov = _all(pg_conn, "SELECT * FROM receipt.provenance WHERE id = %s", b_prov)
        assert (prov[0]["receipt_id"], prov[0]["is_primary"]) == (b.id, True)
        assert _history(pg_conn) == [
            ("link", "manual"),
            ("merge", "manual"),
            ("unlink", "manual"),
            ("unmerge", "manual"),
        ]

    def test_a_moving_primary_hands_over_and_comes_back_non_primary(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        pdf = make_pdf("shared file")
        a = ingest(_drive(pdf))
        second = _drive(pdf)
        ingest(second)  # joins a by rule 1
        c = ingest(_email(), vendor="Other Vendor")
        a_home = _only_prov_where(pg_conn, a.id, primary=True)

        duplicates.link(pg_conn, a_home, c.id, actor=PERSON)
        pg_conn.commit()
        assert _prov(pg_conn, second.source_id).is_primary is True
        assert _receipt(pg_conn, a.id).merged_into_id is None

        duplicates.unlink_home(pg_conn, a_home, actor=PERSON)
        pg_conn.commit()
        rows = _all(
            pg_conn,
            "SELECT id, is_primary FROM receipt.provenance WHERE receipt_id = %s",
            a.id,
        )
        assert {r["id"]: r["is_primary"] for r in rows} == {
            a_home: False,
            _prov(pg_conn, second.source_id).id: True,
        }

    def test_unlink_from_its_own_receipt_or_as_the_only_record_is_refused(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        a = ingest(_email())
        with pytest.raises(duplicates.DuplicateError, match="own receipt"):
            duplicates.unlink_home(pg_conn, _only_prov(pg_conn, a.id), actor=PERSON)
        pg_conn.rollback()

    def test_a_hand_edited_receipt_is_not_merged(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        a, b = _two(ingest)
        pg_conn.execute(
            "UPDATE receipt.receipts SET manual_fields = '{vendor}' WHERE id = %s",
            (b.id,),
        )
        pg_conn.commit()
        with pytest.raises(duplicates.MergeBlockedError, match="hand-edited"):
            duplicates.link(pg_conn, _only_prov(pg_conn, b.id), a.id, actor=PERSON)
        pg_conn.rollback()

    def test_refunds_of_different_purchases_are_not_merged(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        purchase = ingest(_email(), vendor="Shop", order_number="P-1")
        linked = ingest(_email(), vendor="Shop", order_number="P-1", kind="refund")
        assert _receipt(pg_conn, linked.id).refund_of == purchase.id
        loose = ingest(_email(), vendor="Other", kind="refund")
        with pytest.raises(duplicates.MergeBlockedError, match="linked to purchase"):
            duplicates.link(
                pg_conn, _only_prov(pg_conn, linked.id), loose.id, actor=PERSON
            )
        pg_conn.rollback()

    def test_a_link_to_a_merged_receipt_goes_to_the_one_that_remains(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        a = ingest(_email(), vendor="A")
        b = ingest(_email(), vendor="B")
        c = ingest(_email(), vendor="C")
        duplicates.link(pg_conn, _only_prov(pg_conn, b.id), a.id, actor=PERSON)
        pg_conn.commit()
        landed = duplicates.link(pg_conn, _only_prov(pg_conn, c.id), b.id, actor=PERSON)
        pg_conn.commit()
        assert landed == a.id


def _only_prov_where(conn: Conn, receipt_id: uuid.UUID, *, primary: bool) -> uuid.UUID:
    (row,) = _all(
        conn,
        "SELECT id FROM receipt.provenance WHERE receipt_id = %s AND is_primary = %s",
        receipt_id,
        primary,
    )
    rid: uuid.UUID = row["id"]
    return rid


class TestMergeChains:
    def test_merging_into_a_receipt_flattens_what_was_merged_into_it(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        a = ingest(_email(), vendor="A")
        b = ingest(_email(), vendor="B")
        c = ingest(_email(), vendor="C")
        duplicates.link(pg_conn, _only_prov(pg_conn, a.id), b.id, actor=PERSON)
        pg_conn.commit()
        # b now holds a's record and its own; merge b into c.
        duplicates.merge_receipt(pg_conn, b.id, c.id, actor=PERSON, method="manual")
        pg_conn.commit()
        assert _receipt(pg_conn, a.id).merged_into_id == c.id
        assert _receipt(pg_conn, b.id).merged_into_id == c.id

    def test_the_trigger_refuses_a_merge_into_a_merged_receipt(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        a = ingest(_email(), vendor="A")
        b = ingest(_email(), vendor="B")
        c = ingest(_email(), vendor="C")
        duplicates.link(pg_conn, _only_prov(pg_conn, b.id), a.id, actor=PERSON)
        pg_conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="merged receipt"):
            pg_conn.execute(
                "UPDATE receipt.receipts SET merged_into_id = %s, merged_at = now()"
                " WHERE id = %s",
                (b.id, c.id),
            )
        pg_conn.rollback()


class TestHistoryIsAppendOnly:
    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE receipt.provenance_link_history SET actor = 'someone else'",
            "DELETE FROM receipt.provenance_link_history",
            "TRUNCATE receipt.provenance_link_history",
        ],
    )
    def test_the_owner_cannot_change_it(
        self, ingest: _Ingest, pg_conn: Conn, statement: str
    ) -> None:
        a, b = _two(ingest)
        duplicates.link(pg_conn, _only_prov(pg_conn, b.id), a.id, actor=PERSON)
        pg_conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            pg_conn.execute(statement)
        pg_conn.rollback()


# ---------------------------------------------------------------------------
# Totals, search and the other rules
# ---------------------------------------------------------------------------


class TestMergedReceiptsElsewhere:
    def test_search_and_count_leave_merged_out_unless_asked(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        a, b = _two(ingest)
        duplicates.link(pg_conn, _only_prov(pg_conn, b.id), a.id, actor=PERSON)
        pg_conn.commit()
        assert count_receipts(pg_conn) == 1
        assert count_receipts(pg_conn, include_merged=True) == 2
        assert len(search_receipts(pg_conn, include_merged=True)) == 2
        pg_conn.rollback()

    def test_the_view_follows_a_merged_purchase_and_drops_a_merged_refund(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        purchase = ingest(_email(), vendor="Shop", order_number="Q-1")
        refund = ingest(
            _email(),
            vendor="Shop",
            order_number="Q-1",
            kind="refund",
            amount=Decimal("2.00"),
            paid_amount=Decimal("2.00"),
            date=date(2026, 10, 20),
        )
        assert _receipt(pg_conn, refund.id).refund_of == purchase.id
        other = ingest(_email(), vendor="Shop again")
        # The purchase merges into another receipt: its refund follows.
        duplicates.link(
            pg_conn, _only_prov(pg_conn, purchase.id), other.id, actor=PERSON
        )
        pg_conn.commit()
        assert _receipt(pg_conn, other.id).refunded == Decimal("2.00")
        assert _receipt(pg_conn, purchase.id).net_paid is None
        # The refund merges into another refund: it no longer counts.
        spare = ingest(
            _email(),
            vendor="Shop",
            kind="refund",
            amount=Decimal("2.00"),
            paid_amount=Decimal("2.00"),
            date=date(2026, 10, 25),
        )
        pg_conn.execute(
            "UPDATE receipt.receipts SET refund_of = %s WHERE id = %s",
            (purchase.id, spare.id),
        )
        pg_conn.commit()
        duplicates.link(pg_conn, _only_prov(pg_conn, refund.id), spare.id, actor=PERSON)
        pg_conn.commit()
        assert _receipt(pg_conn, other.id).refunded == Decimal("2.00")

    def test_the_refund_rule_skips_a_merged_purchase(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        purchase = ingest(_email(), vendor="Shop", order_number="R-1")
        other = ingest(_email(), vendor="Elsewhere")
        duplicates.link(
            pg_conn, _only_prov(pg_conn, purchase.id), other.id, actor=PERSON
        )
        pg_conn.commit()
        refund = ingest(
            _email(),
            vendor="Shop",
            order_number="R-1",
            kind="refund",
            date=date(2026, 10, 30),
        )
        assert _receipt(pg_conn, refund.id).refund_of is None

    def test_a_merged_refund_links_nothing(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        a = ingest(_email(), vendor="Shop", kind="refund", order_number="S-1")
        b = ingest(_email(), vendor="Other", kind="refund")
        duplicates.link(pg_conn, _only_prov(pg_conn, a.id), b.id, actor=PERSON)
        pg_conn.commit()
        purchase = ingest(
            _email(), vendor="Shop", order_number="S-1", date=date(2026, 10, 1)
        )
        assert link_by_rule(pg_conn, _receipt(pg_conn, a.id)) == []
        pg_conn.rollback()
        assert _receipt(pg_conn, a.id).refund_of is None
        assert _receipt(pg_conn, purchase.id).refunded == Decimal("0")

    def test_reextract_skips_a_merged_receipt(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        a, b = _two(ingest)
        duplicates.link(pg_conn, _only_prov(pg_conn, b.id), a.id, actor=PERSON)
        pg_conn.commit()
        selected = select_for_reextract(
            pg_conn, prompt_version=0, llm_model="other", ids=[a.id, b.id]
        )
        pg_conn.rollback()
        assert [r.id for r in selected] == [a.id]


# ---------------------------------------------------------------------------
# Candidates and the scan
# ---------------------------------------------------------------------------


@contextmanager
def _no_rules() -> Iterator[None]:
    """Ingest as the back catalogue was: before any duplicate rule existed."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(duplicates, "match_original", lambda *a, **k: None)
        mp.setattr(duplicates, "decide", lambda *a, **k: duplicates.Decision())
        yield


class TestCandidates:
    def test_dismiss_then_link_is_refused(self, ingest: _Ingest, pg_conn: Conn) -> None:
        ingest(_email())
        ingest(_email())
        (pair,) = _candidates(pg_conn)
        assert (
            duplicates.decide_candidate(pg_conn, pair["id"], "dismissed", actor=PERSON)
            is None
        )
        pg_conn.commit()
        with pytest.raises(duplicates.DuplicateError, match="already dismissed"):
            duplicates.decide_candidate(pg_conn, pair["id"], "linked", actor=PERSON)
        pg_conn.rollback()
        (row,) = _candidates(pg_conn)
        assert (row["status"], row["decided_by"]) == ("dismissed", "operator")

    def test_linking_a_pair_merges_the_newer_receipt(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        older = ingest(_email())
        newer = ingest(_email())
        (pair,) = _candidates(pg_conn)
        remaining = duplicates.decide_candidate(
            pg_conn, pair["id"], "linked", actor=PERSON
        )
        pg_conn.commit()
        assert remaining == older.id
        assert _receipt(pg_conn, newer.id).merged_into_id == older.id
        assert _candidates(pg_conn)[0]["status"] == "linked"


class TestScan:
    def test_dry_run_writes_nothing_and_a_scan_links_and_flags(
        self, pg_conn: Conn, ingest: _Ingest
    ) -> None:
        five = {"amount": Decimal("5.00"), "paid_amount": Decimal("5.00")}
        with _no_rules():
            first = ingest(_email(), order_number="B-1")
            forward = ingest(_email(), order_number="B-1", vendor=VENDOR.upper())
            ingest(_email(), vendor="Lookalike", **five)
            ingest(_email(), vendor="Lookalike", **five)
        assert len(_live(pg_conn)) == 4

        result = duplicates.scan(pg_conn)
        pg_conn.rollback()
        assert result.links == [(forward.id, first.id, "order_number")]
        assert result.flagged == {"metadata": 1}
        assert len(_live(pg_conn)) == 4
        assert _candidates(pg_conn) == []

        duplicates.scan(pg_conn)
        pg_conn.commit()
        assert len(_live(pg_conn)) == 3
        assert _receipt(pg_conn, forward.id).merged_into_id == first.id
        assert [c["reason"] for c in _candidates(pg_conn)] == ["metadata"]

        again = duplicates.scan(pg_conn)
        pg_conn.commit()
        assert (again.links, again.flags) == ([], [])

    def test_a_pair_dismissed_at_ingest_is_not_flagged_again(
        self, ingest: _Ingest, pg_conn: Conn
    ) -> None:
        ingest(_email())
        ingest(_email())
        (pair,) = _candidates(pg_conn)
        duplicates.decide_candidate(pg_conn, pair["id"], "dismissed", actor=PERSON)
        pg_conn.commit()
        result = duplicates.scan(pg_conn)
        pg_conn.commit()
        assert result.flags == []
        assert len(_candidates(pg_conn)) == 1

    def test_a_blocked_merge_is_flagged(self, pg_conn: Conn, ingest: _Ingest) -> None:
        with _no_rules():
            ingest(_email(), order_number="C-1")
            forward = ingest(_email(), order_number="C-1")
        pg_conn.execute(
            "UPDATE receipt.receipts SET manual_fields = '{description}' WHERE id = %s",
            (forward.id,),
        )
        pg_conn.commit()
        result = duplicates.scan(pg_conn)
        pg_conn.commit()
        assert result.links == []
        assert [c["reason"] for c in _candidates(pg_conn)] == ["merge_blocked"]

    def test_the_window_can_be_widened(self, pg_conn: Conn, ingest: _Ingest) -> None:
        with _no_rules():
            ingest(_email())
            ingest(_email(), date=date(2026, 10, 20))
        assert duplicates.scan(pg_conn).flags == []
        pg_conn.rollback()
        assert len(duplicates.scan(pg_conn, window_days=7).flags) == 1
        pg_conn.rollback()
