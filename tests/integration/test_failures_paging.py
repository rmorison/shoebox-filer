"""`failures --limit/--offset` and its JSON envelope, against the real order."""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from receipt_index.cli import cli
from receipt_index.repository import insert_ingest_log

from .conftest import _DB_DSN
from .test_search_paging import _walk

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg

pytestmark = pytest.mark.integration


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
    """Twelve failed items and three skipped ones, in one transaction.

    One transaction gives every row the same ``created_at``, so only the
    ``id`` tiebreak orders them.
    """
    for n in range(15):
        insert_ingest_log(
            pg_conn,
            source_id=f"<fail-page-{n}@example.com>",
            source_type="imap",
            source_name="test-email",
            status="failed" if n < 12 else "skipped",
            email_subject="Receipt",
            email_sender="billing@example.com",
            error_message=f"failure {n}",
            error_class="content",
            commit=False,
        )
    pg_conn.commit()


def _json(config_file: Path, *args: str) -> Any:
    result = CliRunner().invoke(
        cli, ["--config", str(config_file), "failures", *args, "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.mark.usefixtures("seeded")
def test_unpaged_json_envelope(config_file: Path) -> None:
    data = _json(config_file)
    assert len(data["failures"]) == data["total"] == 12
    assert (data["limit"], data["offset"], data["next_offset"]) == (None, 0, None)
    assert _json(config_file, "--status", "skipped")["total"] == 3


@pytest.mark.usefixtures("seeded")
def test_next_offset_walks_every_page(config_file: Path) -> None:
    seen = _walk(config_file, "failures", "failures")
    # No repeats and no gaps: the walk is the unpaged order.
    assert len(set(seen)) == len(seen) == 12
    assert seen == [f["id"] for f in _json(config_file)["failures"]]


@pytest.mark.usefixtures("seeded")
def test_text_footer_reports_the_page_against_the_total(config_file: Path) -> None:
    base = ["--config", str(config_file), "failures"]
    page = CliRunner().invoke(cli, [*base, "--limit", "5", "--offset", "10"])
    assert page.exit_code == 0, page.output
    assert "2 failed ingest(s) shown (11-12 of 12)." in page.output
    past = CliRunner().invoke(cli, [*base, "--offset", "12"])
    assert "None on this page (12 match)." in past.output
