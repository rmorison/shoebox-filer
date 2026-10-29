"""`search --limit/--offset` pages the real order, without gaps or repeats."""

from __future__ import annotations

import json
import textwrap
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from receipt_index.cli import cli
from receipt_index.repository import insert_receipt

from .conftest import _DB_DSN

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg

_VENDORS = ("Acme Parking (U.S.), LLC", "Example Hardware")


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "receipt-index.yaml"
    path.write_text(
        textwrap.dedent(f"""\
            sources:
              - name: test-email
                type: imap
                host: mail.example.com
                username: user@example.com
                password: unused
            database:
              url: {_DB_DSN}
            store:
              path: {tmp_path / "store"}
            llm:
              api_key: unused
            """)
    )
    return path


@pytest.fixture()
def seeded(pg_conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Twelve receipts, mostly tied on date and vendor, all needing review.

    Only the ``id`` tiebreak orders the receipts inside each (date, vendor)
    group, and with ``--needs-review`` inside each paid-amount confidence.
    """
    for n in range(12):
        insert_receipt(
            pg_conn,
            source_id=f"<paging-{n}@example.com>",
            source_type="imap",
            source_name="test-email",
            vendor=_VENDORS[n % 2],
            amount=Decimal(f"{n + 1}.00"),
            currency="USD",
            receipt_date=date(2025, 6, 10 + n % 3),
            description=None,
            confidence=0.9,
            pdf_path=f"2025/06/paging-{n}.pdf",
            email_subject="Receipt",
            email_sender="billing@example.com",
            email_date=None,
            # No paid amount, so every one needs review; half carry a
            # paid-amount confidence, so that order has ties too.
            field_confidence={"paid_amount": 0.4} if n % 2 else None,
        )


def _ids(config_file: Path, *args: str) -> list[str]:
    result = CliRunner().invoke(
        cli, ["--config", str(config_file), "search", "--output", "json", *args]
    )
    assert result.exit_code == 0, result.output
    return [r["id"] for r in json.loads(result.output)]


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("filters", [(), ("--needs-review",)])
def test_pages_cover_the_unpaged_order(
    config_file: Path, filters: tuple[str, ...]
) -> None:
    everything = _ids(config_file, *filters)
    assert len(everything) == 12

    pages = [
        _ids(config_file, *filters, "--limit", "5"),
        _ids(config_file, *filters, "--limit", "5", "--offset", "5"),
        _ids(config_file, *filters, "--limit", "5", "--offset", "10"),
    ]
    assert [len(p) for p in pages] == [5, 5, 2]
    assert pages[0] + pages[1] + pages[2] == everything
    # An offset alone skips that many and prints the rest.
    assert _ids(config_file, *filters, "--offset", "7") == everything[7:]


@pytest.mark.usefixtures("seeded")
def test_text_footer_reports_the_page_against_the_total(
    config_file: Path, pg_conn: psycopg.Connection[dict[str, Any]]
) -> None:
    base = ["--config", str(config_file), "search"]
    page = CliRunner().invoke(cli, [*base, "--limit", "5", "--offset", "10"])
    assert page.exit_code == 0, page.output
    assert "2 receipt(s) shown (11-12 of 12)." in page.output

    past = CliRunner().invoke(cli, [*base, "--offset", "12"])
    assert "No receipts on this page (12 match)." in past.output

    # A paid amount takes three receipts off the review list: the total
    # counts the same --needs-review filter as the page.
    pg_conn.execute(
        "UPDATE receipt.receipts SET paid_amount = amount "
        "WHERE source_id IN (%s, %s, %s)",
        ("<paging-0@example.com>", "<paging-1@example.com>", "<paging-2@example.com>"),
    )
    pg_conn.commit()
    review = CliRunner().invoke(cli, [*base, "--needs-review", "--limit", "4"])
    assert review.exit_code == 0, review.output
    assert "4 receipt(s) shown (1-4 of 9)." in review.output
