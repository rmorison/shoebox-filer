"""``receipt-index status``: text, JSON and ``--check``, without a database.

The repository reads are patched and the heartbeat file lives in tmp_path,
so these run with or without ``serve`` in the picture.
"""

from __future__ import annotations

import json
import os
import textwrap
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from click.testing import CliRunner

from receipt_index import cli as cli_module
from receipt_index import health, repository
from receipt_index.cli import cli
from receipt_index.health import Pause, WorkerState, write_heartbeat
from receipt_index.models import SyncState

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG = textwrap.dedent("""\
    sources:
      - name: mail
        type: imap
        host: mail.example.com
        username: user@example.com
        password: secret
        folder: INBOX
    database:
      url: postgresql://localhost/test
    llm:
      api_key: test-key
""")


def _row(name: str, folder: str, age: float, error: str | None = None) -> SyncState:
    now = datetime.now(UTC)
    return SyncState(
        id=uuid4(),
        source_name=name,
        source_type="imap",
        folder=folder,
        imap_uidvalidity=7,
        imap_last_uid=42,
        last_poll_at=now - timedelta(seconds=age),
        last_success_at=now - timedelta(seconds=age),
        last_error=error,
        last_error_at=now - timedelta(seconds=age + 60) if error else None,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config = tmp_path / "config.yaml"
    config.write_text(_CONFIG)
    hb = tmp_path / "hb"
    monkeypatch.setattr(health, "HEARTBEAT_PATH", hb)
    state: dict[str, Any] = {
        "config": config,
        "hb": hb,
        "rows": [_row("mail", "INBOX", 30, error="read failed: timeout")],
        "db_ok": True,
    }

    def connect(_config: Any) -> Any:
        def open_conn() -> Any:
            if not state["db_ok"]:
                raise OSError("connection refused")
            return nullcontext(MagicMock())

        return open_conn

    monkeypatch.setattr(cli_module, "_status_connection", connect)
    monkeypatch.setattr(repository, "list_sync_state", lambda _c: state["rows"])
    monkeypatch.setattr(repository, "count_due_retries", lambda _c: 1)
    monkeypatch.setattr(
        repository,
        "count_ingest_failures",
        lambda _c, *, status, since=None: 0 if since and status == "terminal" else 2,
    )
    return state


def _beat(path: Path, *, age: float = 0, **kwargs: Any) -> None:
    kwargs.setdefault("watching", ["mail"])
    write_heartbeat(path, WorkerState(pid=99, written_at=datetime.now(UTC), **kwargs))
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))


def _run(env: dict[str, Any], *args: str) -> Any:
    return CliRunner().invoke(cli, ["--config", str(env["config"]), "status", *args])


class TestCheck:
    def test_healthy_prints_ok_and_exits_0(self, env: dict[str, Any]) -> None:
        _beat(env["hb"])
        result = _run(env, "--check")
        assert result.exit_code == 0, result.output
        assert result.stdout == "ok\n"

    def test_without_serve_the_heartbeat_is_missing(self, env: dict[str, Any]) -> None:
        result = _run(env, "--check")
        assert result.exit_code == 1
        assert result.stdout == "heartbeat missing\n"

    def test_each_problem_is_one_line(self, env: dict[str, Any]) -> None:
        env["rows"] = [_row("mail", "INBOX", 1000)]
        _beat(env["hb"], age=300)
        result = _run(env, "--check")
        assert result.exit_code == 1
        assert result.stdout.splitlines() == [
            "heartbeat stale 300s",
            "source mail stale 1000s",
        ]

    def test_a_lost_lock_and_a_long_pause(self, env: dict[str, Any]) -> None:
        pause = Pause(since=datetime.now(UTC) - timedelta(seconds=800), reason="x")
        _beat(env["hb"], paused={"mail": pause}, lock_lost=["mail"])
        result = _run(env, "--check")
        assert result.exit_code == 1
        assert result.stdout.splitlines() == [
            "source mail lock lost",
            "source mail paused 800s",
        ]

    def test_an_unreachable_database(self, env: dict[str, Any]) -> None:
        env["db_ok"] = False
        _beat(env["hb"])
        result = _run(env, "--check")
        assert result.exit_code == 1
        assert result.stdout == "database unreachable\n"

    def test_json_check_exits_by_the_problems(self, env: dict[str, Any]) -> None:
        result = _run(env, "--check", "--output", "json")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["problems"] == ["heartbeat missing"]


class TestReport:
    def test_text_without_serve(self, env: dict[str, Any]) -> None:
        result = _run(env)
        assert result.exit_code == 0, result.output
        out = result.output
        assert "mail (imap, INBOX)" in out
        assert "cursor: uidvalidity 7, last uid 42" in out
        assert "stale after 720s" in out
        assert "(cleared since): read failed: timeout" in out
        assert "archive: off" in out
        assert "heartbeat: none (serve isn't running here)" in out
        assert "pending 1, retrying 2, queued 2, terminal 2" in out
        assert out.rstrip().endswith("heartbeat missing")

    def test_text_with_serve_and_an_orphan(self, env: dict[str, Any]) -> None:
        env["rows"].append(_row("old-name", "INBOX", 99999))
        pause = Pause(since=datetime.now(UTC) - timedelta(seconds=60), reason="boom")
        _beat(env["hb"], paused={"mail": pause}, busy_seconds=12.0)
        out = _run(env).output
        assert "heartbeat: 0s ago (pid 99)" in out
        assert "watching: mail" in out
        assert "busy: 12s on the current step" in out
        assert "paused: mail for 60s: boom" in out
        assert "Orphaned sync state (no configured source)\n  old-name (INBOX)" in out
        assert out.rstrip().endswith("Problems\n  none")

    def test_json(self, env: dict[str, Any]) -> None:
        _beat(env["hb"])
        result = _run(env, "--output", "json")
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert set(data) == {
            "database",
            "sources",
            "worker",
            "retries",
            "orphans",
            "problems",
        }
        (source,) = data["sources"]
        assert source["cursor"] == {"uidvalidity": 7, "last_uid": 42}
        assert source["stale_after_seconds"] == 720
        assert source["last_error_current"] is False
        assert data["worker"]["pid"] == 99
        assert data["retries"]["pending"] == 1
        assert data["problems"] == []

    def test_an_unreachable_database_exits_1(self, env: dict[str, Any]) -> None:
        env["db_ok"] = False
        _beat(env["hb"])
        result = _run(env)
        assert result.exit_code == 1
        assert "unknown (database unreachable)" in result.output
