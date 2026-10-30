"""Tests for receipt_index.health and the health parts of ``serve``.

The ping tests use a fake monitor on 127.0.0.1 in place of healthchecks.io.
Its URL carries a marker that stands for the capability in a real ping URL:
no log line, error or repr may ever contain it. Every wait is bounded.
"""

from __future__ import annotations

import http.server
import logging
import os
import socket
import threading
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from receipt_index import health, service
from receipt_index.config import AppConfig, IngestConfig
from receipt_index.health import (
    Heartbeat,
    Pause,
    PingLoop,
    Problem,
    SourceStatus,
    WorkerState,
    collect,
    evaluate,
    pause_threshold,
    ping_lines,
    read_heartbeat,
    stale_threshold,
    write_heartbeat,
)
from receipt_index.models import SyncState
from receipt_index.pipeline import IngestResult, RetryResult
from receipt_index.service import HealthLoop, ItemClock, Job

from .test_service import Clock, _drive, _imap, _until, _worker

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_MARKER = "CAPABILITY-MARKER-7f3a"
_ERROR_MARKER = "ERRTEXT-MARKER-91c2"
_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def pipeline_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """The pipeline patched out of the worker, as in test_service."""
    calls = {
        "run_ingest": MagicMock(return_value=IngestResult()),
        "select_due": MagicMock(return_value=[]),
        "run_due_retries": MagicMock(return_value=RetryResult()),
    }
    for name, mock in calls.items():
        monkeypatch.setattr(service, name, mock)
    return calls


def _config(*, sources: list[Any] | None = None, **health_keys: Any) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sources": [
                s.model_dump() for s in (sources or [_imap("mail"), _drive("scans")])
            ],
            "database": {"url": "postgresql://localhost/test"},
            "llm": {"api_key": "test-key"},  # pragma: allowlist secret
            "ingest": {"health": health_keys},
        }
    )


def _row(
    name: str,
    folder: str,
    *,
    source_type: str = "imap",
    success_age: float | None = 10,
    last_error: str | None = None,
) -> SyncState:
    success = None if success_age is None else _NOW - timedelta(seconds=success_age)
    return SyncState(
        id=uuid4(),
        source_name=name,
        source_type=source_type,
        folder=folder,
        last_poll_at=success,
        last_success_at=success,
        last_error=last_error,
        last_error_at=_NOW if last_error else None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _state(**kwargs: Any) -> WorkerState:
    kwargs.setdefault("watching", ["mail", "scans"])
    return WorkerState(pid=1, written_at=_NOW, **kwargs)


def _statuses(config: AppConfig, ages: dict[str, float | None]) -> list[SourceStatus]:
    out = []
    for s in config.sources:
        folder = health.source_folder(s)
        row = (
            _row(s.name, folder, source_type=s.type, success_age=ages[s.name])
            if s.name in ages
            else None
        )
        out.append(
            SourceStatus(
                name=s.name,
                type=s.type,
                folder=folder,
                stale_after_seconds=stale_threshold(s, config.ingest),
                row=row,
            )
        )
    return out


def _lines(
    config: AppConfig,
    *,
    ages: dict[str, float | None] | None = None,
    heartbeat: Heartbeat | None = None,
    database_ok: bool = True,
) -> list[str]:
    ages = {"mail": 10, "scans": 10} if ages is None else ages
    problems = evaluate(
        sources=_statuses(config, ages),
        heartbeat=heartbeat,
        database_ok=database_ok,
        ingest=config.ingest,
        pause_after=pause_threshold(config.sources, config.ingest),
        now=_NOW,
    )
    return [p.line for p in problems]


def _fresh(**kwargs: Any) -> Heartbeat:
    return Heartbeat(age_seconds=5, state=_state(**kwargs))


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_the_stale_threshold_follows_the_poll_interval(self) -> None:
        ingest = IngestConfig()
        assert stale_threshold(_imap(), ingest) == 720
        assert stale_threshold(_drive(), ingest) == 360
        slow = IngestConfig(imap_poll_seconds=3600, drive_poll_seconds=3600)
        assert stale_threshold(_imap(), slow) == 10800
        assert stale_threshold(_drive(), slow) == 10800

    def test_source_stale_seconds_overrides_it(self) -> None:
        ingest = IngestConfig.model_validate({"health": {"source_stale_seconds": 900}})
        assert stale_threshold(_imap(), ingest) == 900
        assert stale_threshold(_drive(), ingest) == 900
        assert pause_threshold([_imap(), _drive()], ingest) == 900

    def test_the_pause_threshold_is_the_largest_stale_threshold(self) -> None:
        assert pause_threshold([_imap(), _drive()], IngestConfig()) == 720
        assert pause_threshold([_drive()], IngestConfig()) == 360


# ---------------------------------------------------------------------------
# The --check conditions
# ---------------------------------------------------------------------------


class TestChecks:
    def test_healthy_has_no_problems(self) -> None:
        assert _lines(_config(), heartbeat=_fresh()) == []

    def test_a_missing_heartbeat(self) -> None:
        assert _lines(_config(), heartbeat=None) == ["heartbeat missing"]

    def test_a_stale_heartbeat(self) -> None:
        old = Heartbeat(age_seconds=340, state=_state())
        assert _lines(_config(), heartbeat=old) == ["heartbeat stale 340s"]

    def test_a_heartbeat_at_the_threshold_is_fresh(self) -> None:
        edge = Heartbeat(age_seconds=120, state=_state())
        assert _lines(_config(), heartbeat=edge) == []

    def test_a_stale_source(self) -> None:
        lines = _lines(_config(), ages={"mail": 1200, "scans": 10}, heartbeat=_fresh())
        assert lines == ["source mail stale 1200s"]

    def test_a_drive_source_goes_stale_sooner(self) -> None:
        lines = _lines(_config(), ages={"mail": 400, "scans": 400}, heartbeat=_fresh())
        assert lines == ["source scans stale 400s"]

    def test_a_source_never_read(self) -> None:
        lines = _lines(_config(), ages={"mail": 10, "scans": None}, heartbeat=_fresh())
        assert lines == ["source scans never succeeded"]
        lines = _lines(_config(), ages={"mail": 10}, heartbeat=_fresh())
        assert lines == ["source scans never succeeded"]

    def test_a_lost_source_lock(self) -> None:
        lines = _lines(_config(), heartbeat=_fresh(lock_lost=["mail"]))
        assert lines == ["source mail lock lost"]

    def test_a_long_pause(self) -> None:
        pause = Pause(since=_NOW - timedelta(seconds=900), reason=_ERROR_MARKER)
        lines = _lines(_config(), heartbeat=_fresh(paused={"mail": pause}))
        assert lines == ["source mail paused 900s"]

    def test_a_short_pause_is_not_a_problem(self) -> None:
        pause = Pause(since=_NOW - timedelta(seconds=600), reason="x")
        assert _lines(_config(), heartbeat=_fresh(paused={"mail": pause})) == []

    def test_an_unreachable_database(self) -> None:
        lines = _lines(_config(), heartbeat=_fresh(), database_ok=False)
        assert lines == ["database unreachable"]

    def test_a_stale_heartbeat_s_content_is_ignored(self) -> None:
        # A restart leaves the old process's file in /tmp: its pauses and lost
        # locks belong to a process that is gone.
        old = Heartbeat(age_seconds=600, state=_state(lock_lost=["mail"]))
        assert _lines(_config(), heartbeat=old) == ["heartbeat stale 600s"]

    def test_only_the_sources_serve_watches_are_checked(self) -> None:
        lines = _lines(
            _config(),
            ages={"mail": 10, "scans": None},
            heartbeat=_fresh(watching=["mail"]),
        )
        assert lines == []

    def test_a_busy_worker_holds_back_the_stale_lines(self) -> None:
        # One worker: a long job leaves the other sources waiting their turn.
        lines = _lines(
            _config(),
            ages={"mail": 1200, "scans": 1200},
            heartbeat=_fresh(busy_seconds=30.0),
        )
        assert lines == []

    def test_without_a_heartbeat_every_source_is_checked(self) -> None:
        lines = _lines(_config(), ages={"mail": 1200, "scans": 10}, heartbeat=None)
        assert lines == ["heartbeat missing", "source mail stale 1200s"]

    def test_a_requested_source_serve_does_not_hold(self) -> None:
        # Another process held its lock at start; serve never takes it later.
        lines = _lines(
            _config(),
            ages={"mail": 10, "scans": 99999},
            heartbeat=_fresh(watching=["mail"], requested=["mail", "scans"]),
        )
        assert lines == ["source scans not held"]

    def test_a_lost_lock_is_not_also_not_held(self) -> None:
        lines = _lines(
            _config(),
            heartbeat=_fresh(
                watching=["mail"], requested=["mail", "scans"], lock_lost=["scans"]
            ),
        )
        assert lines == ["source scans lock lost"]

    def test_a_fresh_start_gets_one_threshold_to_catch_up(self) -> None:
        # Down for an hour, restarted 60 s ago: not stale yet.
        just_started = _NOW - timedelta(seconds=60)
        lines = _lines(
            _config(),
            ages={"mail": 3600, "scans": None},
            heartbeat=_fresh(started_at=just_started),
        )
        assert lines == []

    def test_after_the_catch_up_threshold_it_is_stale_again(self) -> None:
        started = _NOW - timedelta(seconds=800)
        lines = _lines(
            _config(),
            ages={"mail": 3600, "scans": None},
            heartbeat=_fresh(started_at=started),
        )
        assert lines == ["source mail stale 3600s", "source scans never succeeded"]

    def test_the_grace_ends_at_the_first_read_after_start(self) -> None:
        # A serve that keeps restarting: its catch-up read failed, so the
        # old success shows as stale at once.
        config = _config()
        started = _NOW - timedelta(seconds=60)
        statuses = _statuses(config, {"mail": 3600, "scans": 10})
        mail = statuses[0]
        assert mail.row is not None
        mail.row = mail.row.model_copy(
            update={"last_poll_at": _NOW - timedelta(seconds=30)}
        )
        problems = evaluate(
            sources=statuses,
            heartbeat=_fresh(started_at=started),
            database_ok=True,
            ingest=config.ingest,
            pause_after=pause_threshold(config.sources, config.ingest),
            now=_NOW,
        )
        assert [p.line for p in problems] == ["source mail stale 3600s"]

    def test_a_busy_worker_holds_back_the_pause_lines(self) -> None:
        pause = Pause(since=_NOW - timedelta(seconds=900), reason="x")
        lines = _lines(
            _config(), heartbeat=_fresh(paused={"mail": pause}, busy_seconds=5.0)
        )
        assert lines == []

    def test_terminal_failures_never_fail_the_check(self) -> None:
        conn = MagicMock()
        report = _collect(_config(), conn, terminal=5)
        assert report.problems == []


# ---------------------------------------------------------------------------
# collect, and the data-light ping lines
# ---------------------------------------------------------------------------


def _collect(
    config: AppConfig,
    conn: Any,
    *,
    rows: list[SyncState] | None = None,
    terminal: int = 0,
    heartbeat: Path | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> health.Report:
    from receipt_index import repository

    rows = (
        rows
        if rows is not None
        else [_row("mail", "INBOX"), _row("scans", "folder-1", source_type="gdrive")]
    )
    patch = monkeypatch or pytest.MonkeyPatch()
    patch.setattr(repository, "list_sync_state", lambda _c: rows)
    patch.setattr(repository, "count_due_retries", lambda _c: 1)

    def count(_c: Any, *, status: str, since: datetime | None = None) -> int:
        if status == "terminal" and since is not None:
            return terminal
        return {"retrying": 2, "queued": 3, "terminal": 4, "skipped": 5}[status]

    patch.setattr(repository, "count_ingest_failures", count)
    try:
        hb = heartbeat
        if hb is None:
            import tempfile
            from pathlib import Path

            hb = Path(tempfile.mkdtemp()) / "hb"
            write_heartbeat(hb, _state())
        return collect(
            config, lambda: cast("Any", nullcontext(conn)), heartbeat_path=hb, now=_NOW
        )
    finally:
        if monkeypatch is None:
            patch.undo()


class TestCollect:
    def test_rows_match_sources_and_the_rest_are_orphans(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rows = [
            _row("mail", "INBOX"),
            _row("mail", "INBOX.Old"),
            _row("gone", "INBOX"),
        ]
        hb = tmp_path / "hb"
        write_heartbeat(hb, _state())
        report = _collect(
            _config(), MagicMock(), rows=rows, heartbeat=hb, monkeypatch=monkeypatch
        )
        assert report.sources[0].row is rows[0]
        assert report.sources[1].row is None
        assert {(r.source_name, r.folder) for r in report.orphans} == {
            ("mail", "INBOX.Old"),
            ("gone", "INBOX"),
        }
        assert [p.line for p in report.problems] == ["source scans never succeeded"]
        assert report.retries == {
            "pending": 1,
            "retrying": 1,
            "queued": 3,
            "terminal": 4,
            "skipped_7d": 5,
        }

    def test_an_unreachable_database_is_a_problem_not_an_exception(
        self, tmp_path: Path
    ) -> None:
        def refuse() -> Any:
            raise OSError(f"connection refused {_ERROR_MARKER}")

        hb = tmp_path / "hb"
        write_heartbeat(hb, _state())
        report = collect(_config(), refuse, heartbeat_path=hb, now=_NOW)
        assert report.database_ok is False
        assert report.retries is None
        assert [p.line for p in report.problems] == ["database unreachable"]

    def test_the_terminal_count_uses_the_alert_window(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from receipt_index import repository

        seen: list[datetime | None] = []

        def count(_c: Any, *, status: str, since: datetime | None = None) -> int:
            if status == "terminal":
                seen.append(since)
            return 0

        monkeypatch.setattr(repository, "list_sync_state", lambda _c: [])
        monkeypatch.setattr(repository, "count_ingest_failures", count)
        monkeypatch.setattr(repository, "count_due_retries", lambda _c: 0)
        collect(
            _config(terminal_alert_seconds=3600),
            lambda: cast("Any", nullcontext(MagicMock())),
            heartbeat_path=tmp_path / "none",
            now=_NOW,
        )
        assert _NOW - timedelta(seconds=3600) in seen


class TestPingLines:
    def test_a_new_terminal_failure_fails_the_ping(self) -> None:
        report = _collect(_config(), MagicMock(), terminal=2)
        assert ping_lines(report, 86400) == ["terminal 2 in 24h"]
        assert ping_lines(report, 900) == ["terminal 2 in 900s"]

    def test_none_within_the_window_pings_healthy(self) -> None:
        # The window is the query's: once terminal_alert_seconds has passed
        # since the item turned terminal, the count is 0 and the GET resumes.
        report = _collect(_config(), MagicMock(), terminal=0)
        assert ping_lines(report, 86400) == []

    def test_terminal_alerts_off(self) -> None:
        report = _collect(_config(), MagicMock(), terminal=2)
        assert ping_lines(report, 0) == []

    def test_retrying_never_goes_negative(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from receipt_index import repository

        # An item cleared between the pending and retrying queries.
        monkeypatch.setattr(repository, "list_sync_state", lambda _c: [])
        monkeypatch.setattr(repository, "count_due_retries", lambda _c: 3)
        monkeypatch.setattr(
            repository, "count_ingest_failures", lambda _c, *, status, since=None: 2
        )
        report = collect(
            _config(),
            lambda: cast("Any", nullcontext(MagicMock())),
            heartbeat_path=tmp_path / "none",
            now=_NOW,
        )
        assert report.retries is not None
        assert report.retries["retrying"] == 0

    def test_lines_carry_no_error_text_or_pause_reason(self, tmp_path: Path) -> None:
        rows = [
            _row("mail", "INBOX", success_age=5000, last_error=_ERROR_MARKER),
            _row("scans", "folder-1", source_type="gdrive", last_error=_ERROR_MARKER),
        ]
        hb = tmp_path / "hb"
        pause = Pause(since=_NOW - timedelta(seconds=5000), reason=_ERROR_MARKER)
        write_heartbeat(hb, _state(paused={"scans": pause}))
        report = _collect(_config(), MagicMock(), rows=rows, terminal=1, heartbeat=hb)
        lines = ping_lines(report, 86400)
        assert lines == [
            "source mail stale 5000s",
            "source scans paused 5000s",
            "terminal 1 in 24h",
        ]
        assert _ERROR_MARKER not in "\n".join(lines)


# ---------------------------------------------------------------------------
# The heartbeat file
# ---------------------------------------------------------------------------


class TestHeartbeatFile:
    def test_it_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "hb"
        pause = Pause(since=_NOW, reason="read failed")
        state = _state(paused={"mail": pause}, lock_lost=["scans"], busy_seconds=3.5)
        write_heartbeat(path, state)
        read = read_heartbeat(path, now=path.stat().st_mtime + 7)
        assert read == Heartbeat(age_seconds=7, state=state)
        assert [p.name for p in tmp_path.iterdir()] == ["hb"]  # no temp left

    def test_no_file_is_none(self, tmp_path: Path) -> None:
        assert read_heartbeat(tmp_path / "hb") is None

    def test_unreadable_content_still_has_an_age(self, tmp_path: Path) -> None:
        path = tmp_path / "hb"
        path.write_text("{not json")
        read = read_heartbeat(path, now=path.stat().st_mtime + 3)
        assert read == Heartbeat(age_seconds=3, state=None)


# ---------------------------------------------------------------------------
# The heartbeat in the health loop
# ---------------------------------------------------------------------------


class TestHeartbeatLoop:
    def _loop(
        self,
        tmp_path: Path,
        *,
        clock: ItemClock,
        now: Clock,
        exits: list[int] | None = None,
        state: Any = None,
        path: Path | None = None,
    ) -> HealthLoop:
        return HealthLoop(
            clock,
            stop=threading.Event(),
            worker_done=threading.Event(),
            item_timeout_seconds=1200,
            exit_fn=(exits if exits is not None else []).append,
            now=now,
            heartbeat_path=path or tmp_path / "hb",
            state=state or _state,
        )

    def test_written_while_idle(self, tmp_path: Path) -> None:
        now = Clock()
        loop = self._loop(tmp_path, clock=ItemClock(now=now), now=now)
        loop.check()
        assert (tmp_path / "hb").exists()

    def test_written_at_most_every_30_seconds(self, tmp_path: Path) -> None:
        now, states = Clock(), []

        def state() -> WorkerState:
            states.append(1)
            return _state()

        loop = self._loop(tmp_path, clock=ItemClock(now=now), now=now, state=state)
        loop.check()
        now.now += 10
        loop.check()
        now.now += 20
        loop.check()
        assert len(states) == 2

    def test_a_slow_item_keeps_it_fresh(self, tmp_path: Path) -> None:
        now = Clock()
        clock = ItemClock(now=now)
        loop = self._loop(tmp_path, clock=clock, now=now)
        with clock.job(Job("poll", "mail")), clock.running("<slow@example.com>"):
            now.now += 1000
            loop.check()
        assert (tmp_path / "hb").exists()

    def test_a_wedged_item_lets_it_go_stale(self, tmp_path: Path) -> None:
        now, exits = Clock(), cast("list[int]", [])
        clock = ItemClock(now=now)
        loop = self._loop(tmp_path, clock=clock, now=now, exits=exits)
        with clock.job(Job("poll", "mail")), clock.running("<stuck@example.com>"):
            now.now += 1300
            loop.check()  # the watchdog's exit is stubbed, so the loop goes on
        assert exits == [service.WATCHDOG_EXIT_CODE]
        assert not (tmp_path / "hb").exists()

    def test_a_dead_worker_gets_none(self, tmp_path: Path) -> None:
        now = Clock()
        loop = self._loop(tmp_path, clock=ItemClock(now=now), now=now)
        loop.worker_done.set()
        loop.check()
        assert not (tmp_path / "hb").exists()

    def test_a_failed_write_never_stops_the_watchdog(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        now, exits = Clock(), cast("list[int]", [])
        clock = ItemClock(now=now)
        loop = self._loop(
            tmp_path, clock=clock, now=now, exits=exits, path=tmp_path / "no" / "hb"
        )
        with caplog.at_level(logging.WARNING):
            loop.check()
        assert "can't write the heartbeat" in caplog.text
        with clock.job(Job("poll", "mail")):
            now.now += 1300
            loop.check()
        assert exits == [service.WATCHDOG_EXIT_CODE]


class TestWorkerHealthState:
    def test_a_pause_keeps_its_first_start_and_its_latest_reason(self) -> None:
        worker = _worker()
        worker._pause("mail", "first")
        first = worker.health_state()[0]["mail"]
        worker._pause("mail", "second")
        second = worker.health_state()[0]["mail"]
        assert second.since == first.since
        assert second.reason == "second"

    def test_success_clears_the_pause(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        clock = Clock()
        worker = _worker(clock=clock)
        worker._pause("mail", "boom")
        clock.now += 3600
        worker.run_job(Job("poll", "mail"))
        assert worker.health_state() == ({}, [])

    def test_a_dropped_source_is_lock_lost_not_paused(self) -> None:
        worker = _worker()
        worker._pause("mail", "boom")
        worker.dropped.add("mail")
        assert worker.health_state() == ({}, ["mail"])


# ---------------------------------------------------------------------------
# The ping, against a fake monitor
# ---------------------------------------------------------------------------


class FakeMonitor:
    """Records each request: (method, path, body)."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.status = status
        monitor = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                monitor.requests.append((self.command, self.path, body))
                self.send_response(monitor.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

            do_GET = _answer  # noqa: N815
            do_POST = _answer  # noqa: N815

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/{_MARKER}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeMonitor:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(5)


@pytest.fixture
def monitor() -> Iterator[FakeMonitor]:
    with FakeMonitor() as m:
        yield m


def _ping(url: str, lines: list[str] | Exception, **kwargs: Any) -> PingLoop:
    def probe() -> list[str]:
        if isinstance(lines, Exception):
            raise lines
        return lines

    kwargs.setdefault("interval_seconds", 300)
    return PingLoop(url, probe=probe, stop=threading.Event(), **kwargs)


class TestPing:
    def test_healthy_sends_a_get(self, monitor: FakeMonitor) -> None:
        assert _ping(monitor.url, []).ping_once() is True
        assert monitor.requests == [("GET", f"/{_MARKER}", "")]

    def test_problems_post_their_lines_to_fail(self, monitor: FakeMonitor) -> None:
        lines = ["source mail stale 1200s", "terminal 2 in 24h"]
        assert _ping(monitor.url, lines).ping_once() is True
        assert monitor.requests == [
            ("POST", f"/{_MARKER}/fail", "source mail stale 1200s\nterminal 2 in 24h\n")
        ]

    def test_checks_that_raise_post_a_fail_without_the_error(
        self, monitor: FakeMonitor
    ) -> None:
        _ping(monitor.url, RuntimeError(_ERROR_MARKER)).ping_once()
        assert monitor.requests == [
            ("POST", f"/{_MARKER}/fail", "health checks failed\n")
        ]

    def test_a_monitor_error_is_logged_without_the_url(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with FakeMonitor(status=500) as m, caplog.at_level(logging.DEBUG):
            assert _ping(m.url, []).ping_once() is False
        assert "ping not delivered (HTTP 500)" in caplog.text
        assert _MARKER not in caplog.text

    def test_a_closed_port_neither_raises_nor_logs_the_url(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with caplog.at_level(logging.DEBUG):
            ok = _ping(f"http://127.0.0.1:{port}/{_MARKER}", []).ping_once()
        assert ok is False
        assert "ping not delivered" in caplog.text
        assert _MARKER not in caplog.text

    def test_a_silent_monitor_times_out(self, caplog: pytest.LogCaptureFixture) -> None:
        # Accepts the connection, never answers.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        try:
            url = f"http://127.0.0.1:{sock.getsockname()[1]}/{_MARKER}"
            started = time.monotonic()
            with caplog.at_level(logging.DEBUG):
                ok = _ping(url, [], timeout_seconds=0.3).ping_once()
            assert ok is False
            assert time.monotonic() - started < 5
        finally:
            sock.close()
        assert "ping not delivered" in caplog.text
        assert _MARKER not in caplog.text

    def test_a_malformed_url_neither_raises_nor_logs_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # urllib quotes the URL in its own errors for these.
        for url in (
            f"nope://{_MARKER}",
            f"{_MARKER}/no-scheme",
            f"http://127.0.0.1:1/{_MARKER}\n/x",
        ):
            with caplog.at_level(logging.DEBUG):
                assert _ping(url, []).ping_once() is False
        assert _MARKER not in caplog.text

    def test_the_loop_runs_at_once_and_ends_on_stop(self, monitor: FakeMonitor) -> None:
        loop = _ping(monitor.url, [], interval_seconds=300)
        thread = threading.Thread(target=loop.run, daemon=True)
        thread.start()
        assert _until(lambda: len(monitor.requests) == 1)
        loop.stop.set()
        thread.join(5)
        assert not thread.is_alive()

    def test_its_repr_hides_the_url(self, monitor: FakeMonitor) -> None:
        assert _MARKER not in repr(_ping(monitor.url, []))


# ---------------------------------------------------------------------------
# serve: the ping thread and the first heartbeat
# ---------------------------------------------------------------------------


def _service(tmp_path: Path, **kwargs: Any) -> service.Service:
    sources = [_drive("scans")]
    return service.Service(
        sources=sources,
        all_sources=sources,
        locks={"scans": MagicMock()},
        timings=service.ServeTimings(
            idle_reissue_seconds=60,
            imap_poll_seconds=3600,
            drive_poll_seconds=3600,
            item_timeout_seconds=1200,
            join_seconds=5,
        ),
        open_conn=lambda: MagicMock(closed=False, broken=False),
        adapter_factory=MagicMock(),
        agents={"email": MagicMock(), "document": MagicMock()},
        store=MagicMock(),
        llm_model="test-model",
        policy=MagicMock(),
        exit_fn=MagicMock(),
        heartbeat_path=tmp_path / "hb",
        **kwargs,
    )


class TestServeHealth:
    def test_no_ping_url_no_ping_thread(
        self, tmp_path: Path, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        probe = MagicMock(return_value=[])
        svc = _service(tmp_path, ping_url=None, ping_probe=probe)
        svc.start()
        try:
            assert _until(lambda: pipeline_calls["run_ingest"].called)
            names = {t.name for t in threading.enumerate()}
            assert "serve-ping" not in names
        finally:
            svc.stop()
            assert svc.shutdown() is True
        probe.assert_not_called()

    def test_the_first_ping_reads_this_run_s_heartbeat(
        self,
        tmp_path: Path,
        monitor: FakeMonitor,
        pipeline_calls: dict[str, MagicMock],
    ) -> None:
        # A previous process's file, stale and claiming a lost lock.
        old = tmp_path / "hb"
        write_heartbeat(old, _state(lock_lost=["scans"]))
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        seen: list[Heartbeat | None] = []

        def probe() -> list[str]:
            seen.append(read_heartbeat(old))
            return []

        svc = _service(tmp_path, ping_url=monitor.url, ping_probe=probe)
        svc.start()
        try:
            assert _until(lambda: len(monitor.requests) >= 1)
        finally:
            svc.stop()
            assert svc.shutdown() is True
        first = seen[0]
        assert first is not None
        assert first.age_seconds < 60
        assert first.state is not None
        assert first.state.pid == os.getpid()
        assert first.state.watching == ["scans"]
        assert first.state.lock_lost == []
        assert monitor.requests[0][0] == "GET"

    def test_an_unreachable_monitor_never_stops_the_worker(
        self, tmp_path: Path, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        svc = _service(
            tmp_path,
            ping_url=f"http://127.0.0.1:{port}/{_MARKER}",
            ping_probe=lambda: [],
        )
        svc.start()
        try:
            assert _until(lambda: pipeline_calls["run_ingest"].called)
            assert svc.worker_done.is_set() is False
        finally:
            svc.stop()
            assert svc.shutdown() is True


def test_problem_line_shape() -> None:
    assert Problem("heartbeat", "missing").line == "heartbeat missing"
    assert Problem("source", "stale 9s", "mail").line == "source mail stale 9s"


class TestWorkerStateAssembly:
    def test_requested_started_and_a_dropped_source_leaving_watching(
        self, tmp_path: Path, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        svc = _service(tmp_path, requested=["scans", "mail"])
        svc.worker.dropped.add("scans")
        state = svc.worker_state()
        assert state.watching == []
        assert state.lock_lost == ["scans"]
        assert state.requested == ["scans", "mail"]
        assert state.started_at is not None
        assert state.pid == os.getpid()

    def test_requested_defaults_to_the_held_sources(
        self, tmp_path: Path, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        assert _service(tmp_path).worker_state().requested == ["scans"]
