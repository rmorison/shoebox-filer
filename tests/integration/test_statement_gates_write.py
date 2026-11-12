"""Gated accounts at the edges of the columns commit (plan U4, with U2's writer).

Synthetic values only.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from receipt_index.repository import record_original
from receipt_index.statement_extraction import (
    AccountExtraction,
    AccountRun,
    CallUsage,
    FirstPass,
)
from receipt_index.statement_gates import gate_statement
from receipt_index.statement_repository import (
    AccountRow,
    RevisionRow,
    create_statement,
    insert_account,
    insert_revision,
    new_statement_id,
)

if TYPE_CHECKING:
    import psycopg

pytestmark = pytest.mark.integration


def _money(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def test_an_account_whose_difference_overflows_is_held_and_commits(
    pg_conn: psycopg.Connection[dict[str, Any]],
) -> None:
    first = FirstPass.model_validate(
        {
            "document_type": "deposit",
            "accounts": [
                {
                    "label": "x",
                    "account_type": "deposit",
                    "first_page": 1,
                    "last_page": 1,
                }
            ],
        }
    )
    output = AccountExtraction.model_validate(
        {
            "beginning_balance": "999999999999.99",
            "ending_balance": "-999999999999.99",
            "line_order": "oldest_first",
            "lines": [
                {
                    "posted": {"month": 1, "day": 5, "year": 2027},
                    "description": "Synthetic purchase",
                    "amount": "-10.00",
                    "page": 1,
                }
            ],
        }
    )
    gated = gate_statement(
        first, [AccountRun(output=output, capped=False, usage=CallUsage())]
    )
    (account,) = gated.accounts
    assert (account.failure_class, account.difference) == ("does_not_foot", None)

    data = b"synthetic statement bytes"
    digest = hashlib.sha256(data).digest()
    file_id = record_original(
        pg_conn,
        ref="local:originals/2027/01/synthetic.pdf",
        sha256=digest,
        byte_size=len(data),
        media_type="application/pdf",
    )
    statement_id = new_statement_id(pg_conn)
    revision_id, _ = insert_revision(
        pg_conn,
        statement_id,
        RevisionRow(
            origin="ingest",
            document_type="deposit",
            status=gated.status,
            hold_reason=gated.hold_reason,
            pages=1,
        ),
    )
    insert_account(
        pg_conn,
        revision_id,
        AccountRow(
            ordinal=account.ordinal,
            occurrence=account.occurrence,
            account_type=account.account_type,
            status=account.status,
            content_digest=bytes.fromhex(account.content_digest),
            held_by_statement=account.held_by_statement,
            failure_class=account.failure_class,
            difference=_money(account.difference),
            first_page=account.first_page,
            last_page=account.last_page,
            line_order=account.line_order,
            beginning_balance=_money(account.beginning_balance),
            ending_balance=_money(account.ending_balance),
            section_totals=[asdict(t) for t in account.section_totals],
            column_totals=[asdict(t) for t in account.column_totals],
        ),
    )
    create_statement(
        pg_conn,
        statement_id=statement_id,
        original_file_id=file_id,
        source_sha256=digest,
        revision_id=revision_id,
        status=gated.status,
        document_type="deposit",
    )
    pg_conn.commit()
    row = pg_conn.execute(
        "SELECT failure_class, difference, beginning_balance"
        " FROM receipt.statement_accounts"
    ).fetchone()
    assert row is not None
    assert (row["failure_class"], row["difference"]) == ("does_not_foot", None)
    assert row["beginning_balance"] == Decimal("999999999999.99")
