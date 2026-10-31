"""``backfill`` end to end, against GreenMail and the devtest Postgres.

The full mode ingests the unseen messages of a date range, skips the seen
ones, stores a seen receipt's missing original and leaves the sync cursor
alone; a second run does nothing. ``--originals-only`` stores the originals
of back-catalogue receipts and marks the gone ones unavailable, with no LLM
call. A dry run writes nothing and counts what the real run then does. An
item locked by another process (``serve``) is left to it.
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from imapclient import IMAPClient

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.backfill import run_backfill, run_originals_only
from receipt_index.fetching import SourcePool
from receipt_index.locks import lock_key
from receipt_index.repository import insert_ingest_log, save_imap_cursor
from receipt_index.store import LocalEvidenceStore

from .conftest import _DB_DSN, GREENMAIL_IMAP_PORT, seed_email
from .test_e2e_pipeline import _greenmail_imap_config, _unique_user
from .test_originals_ingest import _agent, _pdf

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock

    from receipt_index.config import ImapSourceConfig

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]

_TODAY = datetime.now(tz=UTC).date()
# GreenMail dates each message by its arrival: today, in its own zone.
_SINCE = _TODAY - timedelta(days=1)
_UNTIL = _TODAY + timedelta(days=1)


def _config(user: str, source: str) -> ImapSourceConfig:
    return _greenmail_imap_config(user).model_copy(update={"name": source})


def _mid() -> str:
    return f"<{uuid.uuid4().hex}@example.com>"


def _backfill(
    conn: Conn,
    config: ImapSourceConfig,
    store: LocalEvidenceStore,
    agent: MagicMock,
    **kw: Any,
) -> Any:
    return run_backfill(
        conn,
        ImapAdapter(config),
        store=store,
        source_name=config.name,
        source_type="imap",
        llm_model="test-model",
        agent=agent,
        **{"since": _SINCE, "until": _UNTIL, **kw},
    )


def _originals(
    conn: Conn, config: ImapSourceConfig, store: LocalEvidenceStore, **kw: Any
) -> Any:
    with ExitStack() as stack:
        pool = SourcePool([config], ImapAdapter, stack)  # type: ignore[arg-type]
        return run_originals_only(
            conn, pool, store=store, source_name=config.name, **kw
        )


def _back_catalogue(conn: Conn, source_id: str, source: str) -> Any:
    """A receipt ingested before originals were kept: its original pending."""
    receipt = conn.execute(
        "INSERT INTO receipt.receipts (source_id, source_type, source_name, vendor,"
        " amount, receipt_date, confidence, pdf_path) VALUES (%s, 'imap', %s,"
        " 'Acme Parking (U.S.), LLC', 12.00, '2024-01-15', 0.9, '2024/01/x.pdf')"
        " RETURNING id",
        (source_id, source),
    ).fetchone()
    assert receipt is not None
    prov = conn.execute(
        "INSERT INTO receipt.provenance (receipt_id, is_primary, source_type,"
        " source_name, source_id, received_at, home_receipt_id, original_status,"
        " match_method) VALUES (%s, TRUE, 'imap', %s, %s, '2024-01-15T10:00:00Z',"
        " %s, 'pending', 'migrated') RETURNING id",
        (receipt["id"], source, source_id, receipt["id"]),
    ).fetchone()
    assert prov is not None
    conn.commit()
    return receipt["id"], prov["id"]


def _row(conn: Conn, sql: str, *args: Any) -> dict[str, Any] | None:
    row = conn.execute(sql, args).fetchone()  # type: ignore[arg-type]
    conn.rollback()
    return row


def _count(conn: Conn, sql: str, *args: Any) -> int:
    row = _row(conn, sql, *args)
    assert row is not None
    return int(next(iter(row.values())))


def _served(user: str, message_id: str) -> bytes:
    with IMAPClient("localhost", port=GREENMAIL_IMAP_PORT, ssl=False) as client:
        client.login(user, "any")
        client.select_folder("INBOX", readonly=True)
        (uid,) = client.search(f'HEADER Message-ID "{message_id}"')
        data: bytes = client.fetch([uid], ["BODY.PEEK[]"])[uid][b"BODY[]"]
    return data


def _files(root: Path) -> dict[str, int]:
    return {
        p.relative_to(root).as_posix(): p.stat().st_mtime_ns
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    }


def test_full_run_ingests_the_unseen_and_leaves_the_cursor(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user, source = _unique_user("bf-full"), "mail-bf-full"
    seen, new_a, new_b = _mid(), _mid(), _mid()
    for mid in (seen, new_a, new_b):
        seed_email(
            to_addr=user, subject="Parking", text_body="Total 12", message_id=mid
        )
    insert_ingest_log(
        pg_conn,
        source_id=seen,
        source_type="imap",
        source_name=source,
        status="skipped",
    )
    save_imap_cursor(
        pg_conn, source_name=source, folder="INBOX", uidvalidity=1, last_uid=1
    )
    pg_conn.commit()
    cursor = _row(
        pg_conn, "SELECT * FROM receipt.sync_state WHERE source_name = %s", source
    )
    store = LocalEvidenceStore(tmp_path)

    agent = _agent("body")
    result = _backfill(pg_conn, _config(user, source), store, agent)

    assert (result.listed, result.unseen, result.processed) == (3, 2, 2)
    assert agent.run_sync.call_count == 2
    assert {r.source_id for r in result.receipts} == {new_a, new_b}
    assert (
        _count(
            pg_conn,
            "SELECT count(*) FROM receipt.ingest_log WHERE source_id = %s",
            seen,
        )
        == 1
    )
    assert (
        _row(pg_conn, "SELECT * FROM receipt.sync_state WHERE source_name = %s", source)
        == cursor
    )

    # A second run over the same range does nothing new.
    log_rows = _count(pg_conn, "SELECT count(*) FROM receipt.ingest_log")
    files = _files(tmp_path)
    again = _agent("body")
    second = _backfill(pg_conn, _config(user, source), store, again)
    assert (second.unseen, second.processed, second.originals_stored) == (0, 0, 0)
    again.run_sync.assert_not_called()
    assert _count(pg_conn, "SELECT count(*) FROM receipt.ingest_log") == log_rows
    assert _files(tmp_path) == files


def test_the_range_bounds_the_listing(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user, source = _unique_user("bf-range"), "mail-bf-range"
    seed_email(to_addr=user, subject="Parking", text_body="Total 12", message_id=_mid())
    agent = _agent("body")
    before = _SINCE - timedelta(days=30)
    result = _backfill(
        pg_conn,
        _config(user, source),
        LocalEvidenceStore(tmp_path),
        agent,
        since=before,
        until=_SINCE - timedelta(days=2),
    )
    assert (result.listed, result.unseen) == (0, 0)
    agent.run_sync.assert_not_called()


def test_full_run_stores_a_seen_receipts_missing_original(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user, source = _unique_user("bf-fill"), "mail-bf-fill"
    mid = _mid()
    seed_email(to_addr=user, subject="Parking", text_body="Total 12", message_id=mid)
    _receipt, prov_id = _back_catalogue(pg_conn, mid, source)
    agent = _agent("body")

    result = _backfill(
        pg_conn, _config(user, source), LocalEvidenceStore(tmp_path), agent
    )

    assert (result.unseen, result.originals_missing, result.originals_stored) == (
        0,
        1,
        1,
    )
    agent.run_sync.assert_not_called()
    prov = _row(pg_conn, "SELECT * FROM receipt.provenance WHERE id = %s", prov_id)
    assert prov is not None
    assert prov["original_status"] == "stored"
    assert bytes(prov["source_sha256"]) == hashlib.sha256(_served(user, mid)).digest()


def test_originals_only_stores_found_and_marks_gone(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user, source = _unique_user("bf-orig"), "mail-bf-orig"
    found, gone = _mid(), _mid()
    receipt_pdf = _pdf("receipt")
    seed_email(
        to_addr=user,
        subject="Your parking receipt",
        text_body="Receipt attached.",
        message_id=found,
        attachments=[("receipt.pdf", "application/pdf", receipt_pdf)],
    )
    receipt_id, found_id = _back_catalogue(pg_conn, found, source)
    _r, gone_id = _back_catalogue(pg_conn, gone, source)
    # The receipt's extraction named its document.
    pg_conn.execute(
        "INSERT INTO receipt.extractions (receipt_id, origin, input_kind,"
        " prompt_version, llm_model, output) VALUES (%s, 'ingest', 'source', 1,"
        " 'test-model', '{\"receipt_document\": \"receipt.pdf\"}')",
        (receipt_id,),
    )
    pg_conn.commit()
    store = LocalEvidenceStore(tmp_path)

    result = _originals(pg_conn, _config(user, source), store)

    assert (result.to_fetch, result.stored, result.unavailable, result.failed) == (
        2,
        1,
        1,
        0,
    )
    prov = _row(pg_conn, "SELECT * FROM receipt.provenance WHERE id = %s", found_id)
    assert prov is not None
    assert prov["original_status"] == "stored"
    assert prov["document_certainty"] == "certain"
    served = _served(user, found)
    assert bytes(prov["source_sha256"]) == hashlib.sha256(served).digest()
    files = pg_conn.execute(
        "SELECT pf.position, pf.role, l.locator FROM receipt.provenance_files pf"
        " JOIN receipt.file_locations l ON l.file_id = pf.file_id"
        " WHERE pf.provenance_id = %s ORDER BY pf.position",
        (found_id,),
    ).fetchall()
    pg_conn.rollback()
    assert [(f["position"], f["role"]) for f in files] == [
        (0, "message"),
        (1, "attachment"),
    ]
    assert (tmp_path / files[0]["locator"]).read_bytes() == served
    assert (tmp_path / files[1]["locator"]).read_bytes() == receipt_pdf
    # Dated by the provenance row's received_at.
    assert files[0]["locator"].startswith("originals/2024/01/")

    marked = _row(pg_conn, "SELECT * FROM receipt.provenance WHERE id = %s", gone_id)
    assert marked is not None
    assert marked["original_status"] == "unavailable"
    assert "Message-ID" in marked["original_unavailable_reason"]

    # A re-run finds nothing pending and writes nothing.
    stored = _files(tmp_path)
    again = _originals(pg_conn, _config(user, source), store)
    assert (again.to_fetch, again.stored, again.unavailable) == (0, 0, 0)
    assert _files(tmp_path) == stored


def test_dry_run_writes_nothing_and_counts_what_the_run_does(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user, source = _unique_user("bf-dry"), "mail-bf-dry"
    for _ in range(2):
        seed_email(
            to_addr=user, subject="Parking", text_body="Total 12", message_id=_mid()
        )
    pending = _mid()
    seed_email(
        to_addr=user, subject="Parking", text_body="Total 12", message_id=pending
    )
    _back_catalogue(pg_conn, pending, source)
    store = LocalEvidenceStore(tmp_path)
    log_rows = _count(pg_conn, "SELECT count(*) FROM receipt.ingest_log")

    agent = _agent("body")
    dry = _backfill(pg_conn, _config(user, source), store, agent, dry_run=True)
    orig_dry = _originals(pg_conn, _config(user, source), store, dry_run=True)

    assert (dry.unseen, dry.originals_missing, orig_dry.to_fetch) == (2, 1, 1)
    agent.run_sync.assert_not_called()
    assert _count(pg_conn, "SELECT count(*) FROM receipt.ingest_log") == log_rows
    assert (
        _count(
            pg_conn,
            "SELECT count(*) FROM receipt.provenance"
            " WHERE original_status <> 'pending'",
        )
        == 0
    )
    assert _files(tmp_path) == {}
    assert (
        _count(pg_conn, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
        == 0
    )

    real = _backfill(pg_conn, _config(user, source), store, agent)
    assert real.processed + real.skipped + real.failed == dry.unseen
    assert real.originals_stored == dry.originals_missing


def test_an_item_locked_by_serve_is_left_to_it(
    greenmail_available: bool, pg_conn: Conn, tmp_path: Path
) -> None:
    user, source = _unique_user("bf-lock"), "mail-bf-lock"
    held, free = _mid(), _mid()
    for mid in (held, free):
        seed_email(
            to_addr=user, subject="Parking", text_body="Total 12", message_id=mid
        )

    with psycopg.connect(_DB_DSN, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (lock_key("item", held),))
        result = _backfill(
            pg_conn, _config(user, source), LocalEvidenceStore(tmp_path), _agent("body")
        )

    assert (result.unseen, result.locked, result.processed) == (2, 1, 1)
    assert (
        _count(
            pg_conn,
            "SELECT count(*) FROM receipt.ingest_log WHERE source_id = %s",
            held,
        )
        == 0
    )
    assert (
        _count(
            pg_conn,
            "SELECT count(*) FROM receipt.ingest_log WHERE source_id = %s",
            free,
        )
        >= 1
    )
