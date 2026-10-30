"""Tests for receipt_index.service: the parts of ``serve`` that need no database.

A fake ``imapclient`` drives the IDLE watcher. The worker runs with the
pipeline functions patched out, so each test checks which jobs ran and what
the worker did after them. Every wait in these tests has a timeout, so a bug
fails a test instead of hanging the suite.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from imapclient.exceptions import LoginError

from receipt_index import service
from receipt_index.config import GdriveSourceConfig, ImapSourceConfig
from receipt_index.locks import SourceLockLostError
from receipt_index.pipeline import IngestResult, RetryResult
from receipt_index.service import (
    HealthLoop,
    IdleWatcher,
    ItemClock,
    Job,
    JobQueue,
    Timer,
    Worker,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_WAIT = 5.0  # the longest any test waits for a thread to do something


def _imap(name: str = "mail") -> ImapSourceConfig:
    return ImapSourceConfig(
        name=name,
        host="mail.example.com",
        username="user@example.com",
        password="secret",  # pragma: allowlist secret
    )


def _drive(name: str = "scans") -> GdriveSourceConfig:
    return GdriveSourceConfig(name=name, folder_id="folder-1", token_json="{}")


def _until(check: Callable[[], bool], timeout: float = _WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return check()


class RecordingQueue:
    """Records every job put, without the real queue's de-duplication."""

    def __init__(self) -> None:
        self.puts: list[Job] = []
        self._lock = threading.Lock()

    def put(self, job: Job) -> None:
        with self._lock:
            self.puts.append(job)

    def count(self, job: Job) -> int:
        with self._lock:
            return self.puts.count(job)


# ---------------------------------------------------------------------------
# The IDLE watcher
# ---------------------------------------------------------------------------


class FakeIMAP:
    """A stand-in for ``IMAPClient`` in IDLE.

    ``script`` gives the responses of successive ``idle_check`` calls; an
    exception in it is raised. Once the script runs out, ``idle_check``
    returns nothing, after a short sleep like a quiet server.
    """

    def __init__(self, script: list[Any] | None = None, *, idle: bool = True) -> None:
        self.script = list(script or [])
        self.caps = {"IDLE"} if idle else set()
        self.calls: list[str] = []
        self.sock = MagicMock(spec=socket.socket)
        self._lock = threading.Lock()

    def _call(self, name: str) -> None:
        with self._lock:
            self.calls.append(name)

    def count(self, name: str) -> int:
        with self._lock:
            return self.calls.count(name)

    def socket(self) -> Any:
        return self.sock

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]:
        self._call(f"select:{folder}:{readonly}")
        return {b"UIDVALIDITY": 1}

    def has_capability(self, cap: str) -> bool:
        return cap in self.caps

    def idle(self) -> None:
        self._call("idle")

    def idle_check(self, timeout: float | None = None) -> list[Any]:
        self._call("idle_check")
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return list(item)
        time.sleep(min(timeout or 0.01, 0.01))
        return []

    def idle_done(self) -> tuple[Any, list[Any]]:
        self._call("idle_done")
        return (b"OK", [])

    def logout(self) -> None:
        self._call("logout")


class Connector:
    """Hands out fake clients in turn; an exception in the list is raised."""

    def __init__(self, clients: list[Any]) -> None:
        self.clients = list(clients)
        self.calls = 0

    def __call__(self, config: ImapSourceConfig) -> Any:
        self.calls += 1
        item = self.clients.pop(0) if len(self.clients) > 1 else self.clients[0]
        if isinstance(item, Exception):
            raise item
        return item


@contextmanager
def _watching(
    watcher: IdleWatcher, stop: threading.Event
) -> Iterator[threading.Thread]:
    thread = threading.Thread(target=watcher.run, daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        stop.set()
        thread.join(timeout=_WAIT)
        assert not thread.is_alive(), "the watcher didn't stop within the bound"


def _watcher(
    connect: Connector, queue: RecordingQueue, stop: threading.Event, **kw: Any
) -> IdleWatcher:
    kw.setdefault("reissue_seconds", 60.0)
    kw.setdefault("backoff_first", 0.01)
    kw.setdefault("backoff_max", 0.05)
    kw.setdefault("auth_retry_seconds", 60.0)
    kw.setdefault("check_slice", 0.01)
    return IdleWatcher(_imap(), queue, stop, connect=connect, **kw)  # type: ignore[arg-type]


_POLL = Job("poll", "mail")


class TestIdleWatcher:
    def test_catches_up_on_connect_then_polls_on_exists(self) -> None:
        client = FakeIMAP([[], [(3, b"EXISTS")]])
        queue, stop = RecordingQueue(), threading.Event()
        with _watching(_watcher(Connector([client]), queue, stop), stop):
            assert _until(lambda: queue.count(_POLL) >= 2)
            # Left IDLE to report it, then went back in.
            assert _until(lambda: client.count("idle") >= 2)
        assert "select:INBOX:True" in client.calls  # EXAMINE, read-only
        assert queue.count(_POLL) == 2

    def test_other_responses_queue_nothing(self) -> None:
        client = FakeIMAP([[(1, b"RECENT")], [(2, b"EXPUNGE")]])
        queue, stop = RecordingQueue(), threading.Event()
        with _watching(_watcher(Connector([client]), queue, stop), stop):
            assert _until(lambda: client.count("idle_check") >= 5)
        assert queue.count(_POLL) == 1  # the catch-up only

    def test_idle_is_reissued_when_nothing_happens(self) -> None:
        client = FakeIMAP()
        queue, stop = RecordingQueue(), threading.Event()
        watcher = _watcher(Connector([client]), queue, stop, reissue_seconds=0.05)
        with _watching(watcher, stop):
            assert _until(lambda: client.count("idle") >= 3)
        assert client.count("idle_done") >= 2
        assert queue.count(_POLL) == 1  # a re-issue isn't a poll

    def test_drop_during_idle_reconnects_and_catches_up(self) -> None:
        dropped = FakeIMAP([[], OSError("connection reset")])
        fresh = FakeIMAP()
        connect = Connector([dropped, fresh])
        queue, stop = RecordingQueue(), threading.Event()
        with _watching(_watcher(connect, queue, stop), stop):
            assert _until(lambda: fresh.count("idle") >= 1)
        assert connect.calls == 2
        assert queue.count(_POLL) == 2  # one catch-up per connection
        assert "logout" in dropped.calls

    def test_failed_connects_back_off(self) -> None:
        connect = Connector([OSError("refused"), OSError("refused"), FakeIMAP()])
        queue, stop = RecordingQueue(), threading.Event()
        with _watching(_watcher(connect, queue, stop), stop):
            assert _until(lambda: queue.count(_POLL) >= 1)
        assert connect.calls == 3

    def test_server_without_idle_leaves_polling_to_the_timer(self) -> None:
        client = FakeIMAP(idle=False)
        queue, stop = RecordingQueue(), threading.Event()
        watcher = _watcher(Connector([client]), queue, stop)
        thread = threading.Thread(target=watcher.run, daemon=True)
        thread.start()
        thread.join(timeout=_WAIT)
        try:
            assert not thread.is_alive(), "the watcher should end on its own"
        finally:
            stop.set()
        assert "idle" not in client.calls
        assert queue.count(_POLL) == 1  # the catch-up
        assert "logout" in client.calls

    def test_authentication_error_waits_instead_of_looping(self) -> None:
        connect = Connector([LoginError("[AUTHENTICATIONFAILED] Invalid credentials")])
        queue, stop = RecordingQueue(), threading.Event()
        with _watching(_watcher(connect, queue, stop, auth_retry_seconds=60), stop):
            time.sleep(0.2)
        assert connect.calls == 1
        assert queue.puts == []

    def test_keepalive_is_set_on_the_socket(self) -> None:
        client = FakeIMAP()
        queue, stop = RecordingQueue(), threading.Event()
        with _watching(_watcher(Connector([client]), queue, stop), stop):
            assert _until(lambda: client.count("idle") >= 1)
        client.sock.setsockopt.assert_any_call(
            socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1
        )

    def test_stop_leaves_idle_and_logs_out(self) -> None:
        client = FakeIMAP()
        queue, stop = RecordingQueue(), threading.Event()
        with _watching(_watcher(Connector([client]), queue, stop), stop):
            assert _until(lambda: client.count("idle") >= 1)
        assert client.calls[-2:] == ["idle_done", "logout"]


# ---------------------------------------------------------------------------
# The queue and the timer
# ---------------------------------------------------------------------------


class TestJobQueue:
    def test_a_waiting_job_is_not_queued_twice(self) -> None:
        queue = JobQueue()
        queue.put(Job("poll", "mail"))
        queue.put(Job("poll", "mail"))
        queue.put(Job("retry", "mail"))
        assert queue.get(timeout=0.01) == Job("poll", "mail")
        assert queue.get(timeout=0.01) == Job("retry", "mail")
        assert queue.get(timeout=0.01) is None

    def test_a_taken_job_can_be_queued_again(self) -> None:
        queue = JobQueue()
        queue.put(Job("poll", "mail"))
        assert queue.get(timeout=0.01) == Job("poll", "mail")
        queue.put(Job("poll", "mail"))
        assert queue.get(timeout=0.01) == Job("poll", "mail")

    def test_get_waits_at_most_its_timeout(self) -> None:
        start = time.monotonic()
        assert JobQueue().get(timeout=0.05) is None
        assert time.monotonic() - start < 1.0


class TestTimer:
    def test_queues_each_kind_at_once_and_then_at_its_interval(self) -> None:
        queue, stop = RecordingQueue(), threading.Event()
        timer = Timer(
            [_imap("mail"), _drive("scans")],
            queue,  # type: ignore[arg-type]
            stop,
            imap_poll_seconds=0.05,
            drive_poll_seconds=0.05,
            retry_seconds=0.05,
        )
        thread = threading.Thread(target=timer.run, daemon=True)
        thread.start()
        try:
            assert _until(
                lambda: all(
                    queue.count(j) >= 3
                    for j in (
                        Job("poll", "mail"),
                        Job("poll", "scans"),
                        Job("retry", "mail"),
                        Job("retry", "scans"),
                    )
                )
            )
        finally:
            stop.set()
            thread.join(timeout=_WAIT)
        assert not thread.is_alive()

    def test_long_intervals_queue_only_the_first_round(self) -> None:
        queue, stop = RecordingQueue(), threading.Event()
        timer = Timer(
            [_drive("scans")],
            queue,  # type: ignore[arg-type]
            stop,
            imap_poll_seconds=3600,
            drive_poll_seconds=3600,
            retry_seconds=3600,
        )
        thread = threading.Thread(target=timer.run, daemon=True)
        thread.start()
        try:
            assert _until(lambda: len(queue.puts) >= 2)
            time.sleep(0.1)
        finally:
            stop.set()
            thread.join(timeout=_WAIT)
        assert sorted(queue.puts) == [Job("poll", "scans"), Job("retry", "scans")]


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def pipeline_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    calls = {
        "run_ingest": MagicMock(return_value=IngestResult()),
        "select_due": MagicMock(return_value=[]),
        "run_due_retries": MagicMock(return_value=RetryResult()),
    }
    for name, mock in calls.items():
        monkeypatch.setattr(service, name, mock)
    return calls


def _worker(
    sources: list[Any] | None = None,
    *,
    clock: Clock | None = None,
    open_conn: Callable[[], Any] | None = None,
    locks: dict[str, Any] | None = None,
) -> Worker:
    sources = sources or [_imap("mail"), _drive("scans")]
    return Worker(
        sources=sources,
        locks=locks or {s.name: MagicMock() for s in sources},
        queue=JobQueue(),
        stop=threading.Event(),
        clock=ItemClock(),
        open_conn=open_conn or (lambda: MagicMock(closed=False, broken=False)),
        adapter_factory=lambda cfg: MagicMock(name=f"adapter-{cfg.name}"),
        agents={"email": MagicMock(), "document": MagicMock()},
        store=MagicMock(),
        llm_model="test-model",
        policy=MagicMock(),
        configured=[s.name for s in sources],
        now=clock or Clock(),
    )


class TestWorker:
    def test_poll_job_runs_ingest_for_its_source(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        worker = _worker()
        worker.run_job(Job("poll", "scans"))
        kwargs = pipeline_calls["run_ingest"].call_args.kwargs
        assert kwargs["source_name"] == "scans"
        assert kwargs["source_type"] == "gdrive"
        assert kwargs["source_lock"] is worker.locks["scans"]
        assert kwargs["control"] is worker.clock
        assert kwargs["agent"] is worker.agents["document"]

    def test_every_poll_builds_a_fresh_adapter(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        worker = _worker()
        worker.run_job(Job("poll", "scans"))
        worker.run_job(Job("poll", "scans"))
        first, second = (
            c.kwargs["adapter"] for c in pipeline_calls["run_ingest"].call_args_list
        )
        assert first is not second

    def test_retry_job_runs_due_items_with_a_pool_of_its_own(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        pipeline_calls["select_due"].return_value = [MagicMock()]
        worker = _worker()
        worker.run_job(Job("retry", "mail"))
        worker.run_job(Job("retry", "mail"))
        calls = pipeline_calls["run_due_retries"].call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["own_source"] == "mail"
        assert calls[0].kwargs["control"] is worker.clock
        assert calls[0].kwargs["pool"] is not calls[1].kwargs["pool"]

    def test_retry_job_with_nothing_due_runs_nothing(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        _worker().run_job(Job("retry", "mail"))
        pipeline_calls["run_due_retries"].assert_not_called()

    def test_a_stopped_source_pauses_only_that_source(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        clock = Clock()
        worker = _worker(clock=clock)
        pipeline_calls["run_ingest"].return_value = IngestResult(
            source_error="stopped after a transient failure"
        )
        worker.run_job(Job("poll", "mail"))
        pipeline_calls["run_ingest"].return_value = IngestResult()

        worker.run_job(Job("poll", "mail"))  # paused: skipped
        worker.run_job(Job("poll", "scans"))  # another source: runs
        names = [
            c.kwargs["source_name"] for c in pipeline_calls["run_ingest"].call_args_list
        ]
        assert names == ["mail", "scans"]

        clock.now += service.PAUSE_FIRST_SECONDS + 1
        worker.run_job(Job("poll", "mail"))
        assert pipeline_calls["run_ingest"].call_count == 3

    def test_pauses_double_and_reset_after_a_clean_job(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        clock = Clock()
        worker = _worker(clock=clock)
        stopped = IngestResult(source_error="stopped")
        pipeline_calls["run_ingest"].return_value = stopped
        worker.run_job(Job("poll", "mail"))
        first = worker.paused_until("mail") - clock.now
        clock.now += first + 1
        worker.run_job(Job("poll", "mail"))
        second = worker.paused_until("mail") - clock.now
        assert second == pytest.approx(first * 2)

        clock.now += second + 1
        pipeline_calls["run_ingest"].return_value = IngestResult()
        worker.run_job(Job("poll", "mail"))
        assert worker.paused_until("mail") is None

    def test_pause_is_capped(self, pipeline_calls: dict[str, MagicMock]) -> None:
        clock = Clock()
        worker = _worker(clock=clock)
        pipeline_calls["run_ingest"].return_value = IngestResult(source_error="x")
        for _ in range(12):
            worker.run_job(Job("poll", "mail"))
            clock.now += service.PAUSE_MAX_SECONDS + 1
        worker.run_job(Job("poll", "mail"))
        assert worker.paused_until("mail") - clock.now == service.PAUSE_MAX_SECONDS

    def test_an_exception_in_a_job_pauses_the_source_and_reconnects(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        conns: list[MagicMock] = []

        def open_conn() -> MagicMock:
            conn = MagicMock(closed=False, broken=False)
            conns.append(conn)
            return conn

        worker = _worker(open_conn=open_conn)
        pipeline_calls["run_ingest"].side_effect = RuntimeError("statement timeout")
        worker.run_job(Job("poll", "mail"))  # doesn't raise
        assert worker.paused_until("mail") is not None
        conns[0].closed = True
        pipeline_calls["run_ingest"].side_effect = None
        worker.run_job(Job("poll", "scans"))
        assert len(conns) == 2

    def test_a_lost_lock_is_taken_again(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        lock = MagicMock()
        lock.check.side_effect = SourceLockLostError("gone")
        lock.acquire.return_value = True
        worker = _worker([_imap("mail")], locks={"mail": lock})
        pipeline_calls["run_ingest"].return_value = IngestResult(source_error="lost")
        worker.run_job(Job("poll", "mail"))
        lock.release.assert_called_once()
        lock.acquire.assert_called_once()
        assert "mail" not in worker.dropped

    def test_a_lock_another_process_took_drops_the_source(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        lock = MagicMock()
        lock.check.side_effect = SourceLockLostError("gone")
        lock.acquire.return_value = False
        worker = _worker([_imap("mail")], locks={"mail": lock})
        pipeline_calls["run_ingest"].return_value = IngestResult(source_error="lost")
        worker.run_job(Job("poll", "mail"))
        assert "mail" in worker.dropped
        worker.run_job(Job("poll", "mail"))
        assert pipeline_calls["run_ingest"].call_count == 1

    def test_run_returns_soon_after_stop(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        worker = _worker()
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        worker.stop.set()
        thread.join(timeout=_WAIT)
        assert not thread.is_alive()

    def test_queued_jobs_are_left_once_stopping(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        worker = _worker()
        worker.queue.put(Job("poll", "mail"))
        worker.stop.set()
        worker.run()
        pipeline_calls["run_ingest"].assert_not_called()


# ---------------------------------------------------------------------------
# The item clock and the watchdog
# ---------------------------------------------------------------------------


class TestItemClock:
    def test_running_marks_the_item_and_its_end(self) -> None:
        now = Clock()
        clock = ItemClock(now=now)
        assert clock.busy_since() is None
        with clock.job(Job("poll", "mail")):
            assert clock.busy_since() == (now.now, "poll mail")
            now.now += 5
            with clock.running("<id@example.com>"):
                assert clock.busy_since() == (now.now, "<id@example.com>")
                now.now += 5
            # The listing between items is timed from the last item's end.
            assert clock.busy_since() == (now.now, "poll mail")
        assert clock.busy_since() is None

    def test_should_stop_follows_the_event(self) -> None:
        stop = threading.Event()
        clock = ItemClock(stop=stop)
        assert not clock.should_stop()
        stop.set()
        assert clock.should_stop()


class TestWatchdog:
    def _loop(self, clock: ItemClock, now: Clock, exits: list[int]) -> HealthLoop:
        return HealthLoop(
            clock,
            stop=threading.Event(),
            worker_done=threading.Event(),
            item_timeout_seconds=600,
            exit_fn=exits.append,
            now=now,
        )

    def test_an_item_past_the_timeout_exits_the_process(self) -> None:
        now, exits = Clock(), []
        clock = ItemClock(now=now)
        loop = self._loop(clock, now, exits)
        with clock.job(Job("poll", "mail")), clock.running("<slow@example.com>"):
            now.now += 601
            loop.check()
        assert exits == [service.WATCHDOG_EXIT_CODE]

    def test_a_slow_item_within_the_timeout_is_left_alone(self) -> None:
        now, exits = Clock(), []
        clock = ItemClock(now=now)
        loop = self._loop(clock, now, exits)
        with clock.job(Job("poll", "mail")), clock.running("<slow@example.com>"):
            now.now += 599
            loop.check()
        assert exits == []

    def test_an_idle_worker_never_trips_it(self) -> None:
        now, exits = Clock(), []
        loop = self._loop(ItemClock(now=now), now, exits)
        now.now += 100000
        loop.check()
        assert exits == []

    def test_a_listing_past_the_timeout_exits_too(self) -> None:
        now, exits = Clock(), []
        clock = ItemClock(now=now)
        loop = self._loop(clock, now, exits)
        with clock.job(Job("poll", "mail")):
            now.now += 601
            loop.check()
        assert exits == [service.WATCHDOG_EXIT_CODE]

    def test_keeps_watching_until_the_worker_is_done(self) -> None:
        now, exits = Clock(), []
        clock = ItemClock(now=now)
        stop, done = threading.Event(), threading.Event()
        loop = HealthLoop(
            clock,
            stop=stop,
            worker_done=done,
            item_timeout_seconds=600,
            exit_fn=exits.append,
            now=now,
            interval_seconds=0.01,
        )
        thread = threading.Thread(target=loop.run, daemon=True)
        thread.start()
        stop.set()
        time.sleep(0.05)
        assert thread.is_alive(), "the watchdog must outlive the stop event"
        done.set()
        thread.join(timeout=_WAIT)
        assert not thread.is_alive()


class TestWorkerSurvivesADatabaseOutage:
    def test_a_failed_lock_retake_pauses_the_source_and_keeps_the_worker(
        self, pipeline_calls: dict[str, MagicMock]
    ) -> None:
        import psycopg

        lock = MagicMock()
        lock.check.side_effect = SourceLockLostError("gone")
        lock.acquire.side_effect = psycopg.OperationalError("connection refused")
        worker = _worker([_imap("mail")], locks={"mail": lock})
        pipeline_calls["run_ingest"].side_effect = psycopg.OperationalError("down")

        worker.run_job(Job("poll", "mail"))  # must not raise

        assert "mail" not in worker.dropped
        assert worker.paused_until("mail") is not None

    def test_an_unexpected_error_in_a_job_never_ends_the_worker(
        self, pipeline_calls: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = _worker()
        calls: list[Job] = []

        def boom(job: Job) -> None:
            calls.append(job)
            raise RuntimeError("a bug")

        monkeypatch.setattr(worker, "run_job", boom)
        worker.queue.put(Job("poll", "mail"))
        worker.queue.put(Job("poll", "scans"))
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        try:
            assert _until(lambda: len(calls) == 2)
            assert thread.is_alive()
        finally:
            worker.stop.set()
            thread.join(timeout=_WAIT)
        assert not thread.is_alive()


class TestDeadWorkerIsFatal:
    def test_a_worker_that_ends_on_its_own_stops_the_service(self) -> None:
        sources = [_drive("scans")]
        svc = service.Service(
            sources=sources,
            all_sources=sources,
            locks={"scans": MagicMock()},
            timings=service.ServeTimings(
                idle_reissue_seconds=60,
                imap_poll_seconds=3600,
                drive_poll_seconds=3600,
                item_timeout_seconds=600,
                join_seconds=1,
            ),
            open_conn=MagicMock(),
            adapter_factory=MagicMock(),
            agents={"email": MagicMock(), "document": MagicMock()},
            store=MagicMock(),
            llm_model="test-model",
            policy=MagicMock(),
            exit_fn=MagicMock(),
        )

        def crash() -> None:
            raise RuntimeError("worker crashed")

        svc.worker.run = crash  # type: ignore[method-assign]
        svc.start()
        assert svc.stop_event.wait(_WAIT), "a dead worker must stop the service"
        assert svc.shutdown() is False  # reported as a failed run (exit 1)


class TestIdleBackoffResets:
    def test_a_healthy_session_resets_the_reconnect_delay(self) -> None:
        # Two drops after healthy sessions: each reconnect waits the first
        # delay, not a doubled one.
        healthy_then_drop = [
            FakeIMAP([[(1, b"EXISTS")], OSError("reset")]) for _ in range(3)
        ]
        connect = Connector([*healthy_then_drop, FakeIMAP()])
        queue, stop = RecordingQueue(), threading.Event()
        waits: list[float] = []
        real_wait = stop.wait

        def recording_wait(timeout: float | None = None) -> bool:
            if timeout is not None and timeout >= 0.5:
                waits.append(timeout)
            return real_wait(0.001)

        stop.wait = recording_wait  # type: ignore[method-assign]
        watcher = _watcher(connect, queue, stop, backoff_first=1.0, backoff_max=8.0)
        with _watching(watcher, stop):
            assert _until(lambda: connect.calls >= 4)
        assert waits[:3] == [1.0, 1.0, 1.0]
