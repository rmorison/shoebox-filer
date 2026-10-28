"""The application's write paths run as a login that holds only the _write grants.

Production runs three logins (Database Standards: _all, _write, _read). The
application logs in as a member of ``receipt_index_dev_write`` and nothing
else; backups, forensics and the MCP server as a member of
``receipt_index_dev_read``. Every other integration test runs as the
superuser, so none of them would notice a missing grant.

Here the migrations' own grants are applied (``apply_migration_grants``), then
the CLI runs as two test logins: ``ingest`` (a success and a failure, with
the retry columns), ``retry --yes``, a second ``ingest`` that runs the
requeued item, and ``reextract``; the Drive cursor is saved directly, since
CI has no Drive. Removing a grant that one of these needs from a migration
file fails this module.

The logins are created here, as ``main``, because CI's Postgres has no
application roles. Roles are cluster-level, so they outlive the run; each
run resets their memberships and checks them before trusting a pass.
"""

from __future__ import annotations

import json
import textwrap
import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import psycopg
import pytest
from click.testing import CliRunner
from psycopg import errors, sql
from pydantic_ai.exceptions import UnexpectedModelBehavior

from receipt_index.cli import cli
from receipt_index.mcp_server import startup_check
from receipt_index.models import FieldConfidence, Payment, ReceiptMetadata
from receipt_index.repository import save_drive_cursor

from .conftest import (
    _POSTGRES_HOST_PORT,
    GRANT_ROLES,
    GREENMAIL_IMAP_PORT,
    apply_migration_grants,
    seed_email,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]

_PASSWORD = "localpass"  # pragma: allowlist secret
_WRITE_LOGIN = "receipt_index_it_write"
_READ_LOGIN = "receipt_index_it_read"
# Each login and the one grant-target role it may belong to.
_LOGINS = {
    _WRITE_LOGIN: "receipt_index_dev_write",
    _READ_LOGIN: "receipt_index_dev_read",
}


def _dsn(login: str) -> str:
    return (
        f"postgresql://{login}:{_PASSWORD}@localhost:"
        f"{_POSTGRES_HOST_PORT}/receipt_index_dev"
    )


def _memberships(conn: Conn, login: str) -> set[str]:
    rows = conn.execute(
        """\
        SELECT g.rolname
          FROM pg_auth_members m
          JOIN pg_roles r ON r.oid = m.member
          JOIN pg_roles g ON g.oid = m.roleid
         WHERE r.rolname = %s
        """,
        (login,),
    ).fetchall()
    return {r["rolname"] for r in rows}


def _ensure_login(conn: Conn, login: str, member_of: str) -> None:
    """A plain login, a member of ``member_of`` and of nothing else."""
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)
    ).fetchone()
    verb = "ALTER" if exists else "CREATE"
    conn.execute(
        sql.SQL(
            "{} ROLE {} WITH LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOBYPASSRLS PASSWORD {}"
        ).format(sql.SQL(verb), sql.Identifier(login), sql.Literal(_PASSWORD))
    )
    # No privileges of its own: only the membership may supply them.
    conn.execute(
        sql.SQL("REVOKE ALL ON SCHEMA receipt FROM {}").format(sql.Identifier(login))
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA receipt FROM {}").format(
            sql.Identifier(login)
        )
    )
    for other in _memberships(conn, login) - {member_of}:
        conn.execute(
            sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(other), sql.Identifier(login)
            )
        )
    conn.execute(
        sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(member_of), sql.Identifier(login)
        )
    )


@pytest.fixture()
def logins(pg_conn: Conn) -> None:
    """The migrations' grants, and the two test logins."""
    pg_conn.rollback()
    apply_migration_grants(pg_conn)
    for login, member_of in _LOGINS.items():
        _ensure_login(pg_conn, login, member_of)
    pg_conn.commit()


@pytest.fixture()
def write_conn(logins: None) -> Iterator[Conn]:
    conn = psycopg.connect(_dsn(_WRITE_LOGIN), row_factory=psycopg.rows.dict_row)
    yield conn
    conn.close()


@pytest.fixture()
def read_conn(logins: None) -> Iterator[Conn]:
    conn = psycopg.connect(_dsn(_READ_LOGIN), row_factory=psycopg.rows.dict_row)
    yield conn
    conn.close()


def test_logins_hold_only_their_grant_role(logins: None, pg_conn: Conn) -> None:
    """Checked first, so a pass below can't come from extra privilege."""
    for login, member_of in _LOGINS.items():
        assert _memberships(pg_conn, login) == {member_of}
        role = pg_conn.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
            "FROM pg_roles WHERE rolname = %s",
            (login,),
        ).fetchone()
        assert role == {
            "rolsuper": False,
            "rolbypassrls": False,
            "rolcreaterole": False,
            "rolcreatedb": False,
        }
        owned = pg_conn.execute(
            "SELECT count(*) AS n FROM pg_class c JOIN pg_roles r "
            "ON r.oid = c.relowner WHERE r.rolname = %s",
            (login,),
        ).fetchone()
        assert owned == {"n": 0}
    # The grant-target roles themselves are members of nothing.
    for role_name in GRANT_ROLES:
        assert _memberships(pg_conn, role_name) == set()
    pg_conn.commit()


# ---------------------------------------------------------------------------
# The write login: every write path the application has
# ---------------------------------------------------------------------------


def _meta(subject: str) -> ReceiptMetadata:
    return ReceiptMetadata(
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal("12.50"),
        paid_amount=Decimal("12.50"),
        date=date(2026, 10, 1),
        description=subject,
        confidence=0.95,
        field_confidence=FieldConfidence(
            vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9
        ),
    )


class _Llm:
    """A mock agent: content failure while ``failing`` names the subject.

    It stands in for extraction only, so the write paths run without an API
    key; it says nothing about the real LLM stack.
    """

    def __init__(self) -> None:
        self.failing: set[str] = set()
        self.output = _meta("ok")
        self.agent = MagicMock()
        self.agent.run_sync.side_effect = self._run

    def _run(self, message: Any, *args: Any, **kwargs: Any) -> MagicMock:
        text = message if isinstance(message, str) else str(message[0])
        for subject in self.failing:
            if subject in text:
                raise UnexpectedModelBehavior("output validation failed")
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        usage.cache_read_tokens = 0
        usage.requests = 1
        result = MagicMock()
        result.output = self.output
        result.usage = usage
        return result


def _config(tmp_path: Path, *, login: str, user: str, source: str) -> Path:
    (tmp_path / "store").mkdir(exist_ok=True)
    path = tmp_path / f"{login}.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            sources:
              - name: {source}
                type: imap
                host: localhost
                port: {GREENMAIL_IMAP_PORT}
                username: {user}
                password: any
                folder: INBOX
                use_ssl: false
            database:
              url: {_dsn(login)}
            store:
              path: {tmp_path / "store"}
            llm:
              api_key: not-used
            """
        )
    )
    return path


def _invoke(config: Path, *args: str) -> Any:
    result = CliRunner().invoke(cli, ["--config", str(config), *args])
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def _statuses(conn: Conn, source_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT status FROM receipt.ingest_log WHERE source_id = %s "
        "ORDER BY created_at, id",
        (source_id,),
    ).fetchall()
    conn.commit()
    return [r["status"] for r in rows]


def test_write_paths_run_as_the_write_login(
    greenmail_available: bool,
    logins: None,
    pg_conn: Conn,
    write_conn: Conn,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _Llm()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    monkeypatch.setattr(
        "receipt_index.extraction.create_extraction_agent",
        lambda *a, **kw: llm.agent,
    )
    user = f"logins-{uuid.uuid4().hex[:8]}@localhost"
    source = f"logins-{uuid.uuid4().hex[:6]}"
    config = _config(tmp_path, login=_WRITE_LOGIN, user=user, source=source)
    good_id = f"<{uuid.uuid4().hex}@example.com>"
    bad_id = f"<{uuid.uuid4().hex}@example.com>"
    seed_email(
        to_addr=user,
        subject="Parking good",
        text_body="Total: $12.50",
        message_id=good_id,
    )
    seed_email(
        to_addr=user,
        subject="Parking bad",
        text_body="Total: $12.50",
        message_id=bad_id,
    )

    # 1. ingest: receipts, extractions, ingest_log (started, success, failed
    #    with the retry columns), the IMAP cursor, record_poll, the locks.
    llm.failing = {"Parking bad"}
    result = _invoke(config, "ingest", "--source", source)
    assert result.exit_code in (0, 1), result.output
    assert _statuses(pg_conn, good_id) == ["started", "success"]
    assert _statuses(pg_conn, bad_id) == ["started", "failed"]
    failed = pg_conn.execute(
        "SELECT attempt, error_class, next_retry_at FROM receipt.ingest_log "
        "WHERE source_id = %s AND status = 'failed'",
        (bad_id,),
    ).fetchone()
    assert failed is not None and failed["error_class"] == "content"
    assert failed["next_retry_at"] is not None
    receipt = pg_conn.execute(
        "SELECT id FROM receipt.receipts WHERE source_id = %s", (good_id,)
    ).fetchone()
    assert receipt is not None
    state = pg_conn.execute(
        "SELECT imap_last_uid, last_poll_at, last_success_at "
        "FROM receipt.sync_state WHERE source_name = %s",
        (source,),
    ).fetchone()
    assert state is not None and state["imap_last_uid"] is not None
    assert state["last_poll_at"] is not None
    pg_conn.commit()

    # 2. retry --yes: a retry_requests row, its count, and a 'queued' row.
    result = _invoke(config, "retry", bad_id, "--yes")
    assert result.exit_code == 0, result.output
    assert _statuses(pg_conn, bad_id)[-1] == "queued"
    request = pg_conn.execute(
        "SELECT db_user, matched, requeued FROM receipt.retry_requests"
    ).fetchone()
    assert request == {"db_user": _WRITE_LOGIN, "matched": 1, "requeued": 1}
    pg_conn.commit()

    # 3. ingest again: the requeued item runs, as a retry, and succeeds.
    llm.failing = set()
    result = _invoke(config, "ingest", "--source", source)
    assert result.exit_code == 0, result.output
    assert _statuses(pg_conn, bad_id)[-1] == "success"

    # 4. reextract: reads the receipt FOR UPDATE, rewrites every column it
    #    may write (each needs its UPDATE grant) and appends to the history.
    #    The new answer differs in every field, or reextract writes nothing.
    llm.output = ReceiptMetadata(
        vendor="Acme Parking (U.S.), LLC, Garage 2",
        amount=Decimal("20.00"),
        paid_amount=Decimal("18.00"),
        currency="EUR",
        date=date(2026, 10, 2),
        description="re-extracted",
        order_number="A-100",
        payment=Payment(method="card", last4="1234", payer="Jane Doe"),
        confidence=0.99,
        field_confidence=FieldConfidence(
            vendor=0.8, amount=0.8, paid_amount=0.8, date=0.8, payment=0.8
        ),
    )
    before = pg_conn.execute(
        "SELECT count(*) AS n FROM receipt.extractions WHERE receipt_id = %s",
        (receipt["id"],),
    ).fetchone()
    pg_conn.commit()
    result = _invoke(
        config,
        "reextract",
        "--id",
        str(receipt["id"]),
        "--force",
        "--overwrite",
        "--input",
        "rendition",
    )
    assert result.exit_code == 0, result.output
    after = pg_conn.execute(
        "SELECT count(*) AS n FROM receipt.extractions WHERE receipt_id = %s",
        (receipt["id"],),
    ).fetchone()
    assert before is not None and after is not None
    assert after["n"] == before["n"] + 1
    row = pg_conn.execute(
        "SELECT vendor, amount, paid_amount, currency, receipt_date, description, "
        "order_number, payment_method, payment_last4, payer, "
        "field_confidence FROM receipt.receipts WHERE id = %s",
        (receipt["id"],),
    ).fetchone()
    assert row == {
        "vendor": "Acme Parking (U.S.), LLC, Garage 2",
        "amount": Decimal("20.00"),
        "paid_amount": Decimal("18.00"),
        "currency": "EUR",
        "receipt_date": date(2026, 10, 2),
        "description": "re-extracted",
        "order_number": "A-100",
        "payment_method": "card",
        "payment_last4": "1234",
        "payer": "Jane Doe",
        "field_confidence": {
            "vendor": 0.8,
            "amount": 0.8,
            "paid_amount": 0.8,
            "date": 0.8,
            "payment": 0.8,
        },
    }, result.output
    pg_conn.commit()

    # 4b. edit, edit --clear-manual and history: the hand-edit columns,
    #     manual_fields, field_confidence, and receipt_events (INSERT, SELECT).
    rid = str(receipt["id"])
    result = _invoke(
        config,
        "edit",
        rid,
        "--paid-amount",
        "17.50",
        "--order-number",
        "A-101",
        "--reason",
        "statement",
    )
    assert result.exit_code == 0, result.output
    result = _invoke(config, "edit", rid, "--clear-manual", "paid_amount")
    assert result.exit_code == 0, result.output
    result = _invoke(config, "history", rid, "--output", "json")
    assert result.exit_code == 0, result.output
    events = json.loads(result.output)
    assert [(e["event_type"], e["payload"]["field"]) for e in events] == [
        ("field_edited", "paid_amount"),
        ("field_edited", "order_number"),
        ("manual_cleared", "paid_amount"),
    ]
    assert {e["actor"] for e in events} == {"operator"}
    row = pg_conn.execute(
        "SELECT paid_amount, order_number, manual_fields FROM receipt.receipts "
        "WHERE id = %s",
        (receipt["id"],),
    ).fetchone()
    assert row == {
        "paid_amount": Decimal("17.50"),
        "order_number": "A-101",
        "manual_fields": ["order_number"],
    }
    pg_conn.commit()

    # 4c. edit --kind and the refund commands: kind and refund_of (UPDATE),
    #     the receipt_net view and the rule's and a person's events.
    purchase = pg_conn.execute(
        "INSERT INTO receipt.receipts (source_id, source_name, vendor, amount, "
        "paid_amount, currency, receipt_date, confidence, pdf_path, order_number) "
        "VALUES (%s, %s, 'Acme Parking (U.S.), LLC, Garage 2', 20.00, 20.00, "
        "'EUR', '2026-10-01', 0.9, 'p.pdf', 'A-101') RETURNING id",
        (f"<purchase-{source}@example.com>", source),
    ).fetchone()
    assert purchase is not None
    pg_conn.commit()
    result = _invoke(config, "edit", rid, "--kind", "refund")
    assert result.exit_code == 0, result.output
    assert "linked to purchase" in result.output
    result = _invoke(config, "refund", "unlink", rid, "--reason", "check")
    assert result.exit_code == 0, result.output
    result = _invoke(config, "refund", "link", rid, str(purchase["id"]))
    assert result.exit_code == 0, result.output
    result = _invoke(config, "search", "--kind", "purchase", "--output", "json")
    assert result.exit_code == 0, result.output
    (shown,) = [r for r in json.loads(result.output) if r["id"] == str(purchase["id"])]
    assert shown["net_paid"] == "2.50"
    events = pg_conn.execute(
        "SELECT event_type, actor_kind FROM receipt.receipt_events "
        "WHERE receipt_id = %s AND event_type LIKE 'refund%%' "
        "ORDER BY created_at, id",
        (receipt["id"],),
    ).fetchall()
    assert [(e["event_type"], e["actor_kind"]) for e in events] == [
        ("refund_linked", "rule"),
        ("refund_unlinked", "person"),
        ("refund_linked", "person"),
    ]
    pg_conn.commit()

    # 5. The Drive cursor (CI has no Drive to ingest from).
    for reset in (False, True):
        save_drive_cursor(
            write_conn,
            source_name=f"{source}-drive",
            folder="receipts",
            page_token="token",
            account_id="account",
            reset=reset,
        )
    resets = pg_conn.execute(
        "SELECT resets FROM receipt.sync_state WHERE source_name = %s",
        (f"{source}-drive",),
    ).fetchone()
    assert resets == {"resets": 1}
    pg_conn.commit()


@pytest.mark.parametrize(
    "statement",
    [
        # The append-only guard: only the owner can switch it off.
        "ALTER TABLE receipt.extractions DISABLE TRIGGER trg_extractions_append_only",
        "ALTER TABLE receipt.receipt_events "
        "DISABLE TRIGGER trg_receipt_events_append_only",
        # receipt_events: INSERT and SELECT only.
        "UPDATE receipt.receipt_events SET actor = 'someone else'",
        "DELETE FROM receipt.receipt_events",
        "TRUNCATE receipt.receipt_events",
        "DELETE FROM receipt.ingest_log",
        "TRUNCATE receipt.ingest_log",
        # ingest_log is append-only; 000014 takes back 000007's UPDATE grant.
        "UPDATE receipt.ingest_log SET llm_model = 'changed'",
        "CREATE TABLE receipt.not_allowed (id INTEGER)",
    ],
)
def test_write_login_cannot_change_history_or_schema(
    write_conn: Conn, statement: str
) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        write_conn.execute(statement)  # type: ignore[arg-type]
    write_conn.rollback()


# ---------------------------------------------------------------------------
# The read login: backups, forensics, the MCP server
# ---------------------------------------------------------------------------


def test_read_login_can_read_every_table_pg_dump_reads(read_conn: Conn) -> None:
    """pg_dump takes ACCESS SHARE on every table, which needs SELECT."""
    tables = read_conn.execute(
        """\
        SELECT format('%I.%I', n.nspname, c.relname) AS name
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname IN ('public', 'receipt') AND c.relkind IN ('r', 'p')
         ORDER BY 1
        """
    ).fetchall()
    names = [t["name"] for t in tables]
    assert "public.schema_migrations_public" in names
    assert "receipt.receipts" in names
    for name in names:
        read_conn.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE").format(sql.SQL(name))
        )
        read_conn.execute(sql.SQL("SELECT * FROM {} LIMIT 1").format(sql.SQL(name)))
    read_conn.rollback()


def test_read_login_passes_the_mcp_check_and_cannot_write(read_conn: Conn) -> None:
    startup_check(_dsn(_READ_LOGIN))
    with pytest.raises(errors.InsufficientPrivilege):
        read_conn.execute(
            "INSERT INTO receipt.ingest_log (source_id, source_type, status) "
            "VALUES ('x', 'imap', 'started')"
        )
    read_conn.rollback()


def test_write_login_fails_the_mcp_check(write_conn: Conn) -> None:
    from receipt_index.mcp_server import RoleCheckError

    with pytest.raises(RoleCheckError, match="can write"):
        startup_check(_dsn(_WRITE_LOGIN))


def test_forensic_commands_run_as_the_read_login(
    logins: None, pg_conn: Conn, tmp_path: Path
) -> None:
    source = f"logins-read-{uuid.uuid4().hex[:6]}"
    config = _config(
        tmp_path, login=_READ_LOGIN, user="nobody@localhost", source=source
    )
    pg_conn.execute(
        "INSERT INTO receipt.receipts (source_id, source_name, vendor, amount, "
        "receipt_date, confidence, pdf_path) VALUES "
        "('<read@example.com>', %s, 'Acme Parking (U.S.), LLC', 1.00, "
        "'2026-10-01', 0.9, 'x.pdf') RETURNING id",
        (source,),
    )
    receipt_id = pg_conn.execute(
        "SELECT id FROM receipt.receipts WHERE source_id = '<read@example.com>'"
    ).fetchone()
    pg_conn.commit()
    assert receipt_id is not None
    for args in (
        ("search", "--vendor", "Acme", "--output", "json"),
        (
            "search",
            "--order-number",
            "A-100",
            "--last4",
            "1234",
            "--payer",
            "jane",
            "--output",
            "json",
        ),
        ("search", "--kind", "refund", "--kind", "statement", "--output", "json"),
        ("refund", "list", "--output", "json"),
        ("failures", "--output", "json"),
        ("show", str(receipt_id["id"])),
        ("history", str(receipt_id["id"])),
    ):
        result = _invoke(config, *args)
        assert result.exit_code == 0, (args, result.output)
