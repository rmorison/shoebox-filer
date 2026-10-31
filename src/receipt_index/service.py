"""``receipt-index serve``: always-on ingest (always-on ingest design, ticket 3).

The process runs these threads, all daemons, so a thread stuck in a blocking
call can never hold the process open:

- **One IDLE watcher per IMAP source**, on a connection of its own. It
  ``EXAMINE``s the folder, sits in IDLE and queues a poll of its source when
  the server reports ``EXISTS``. It touches neither Postgres nor the cursor.
- **One timer.** It queues a poll of each Drive source every
  ``drive_poll_seconds``, a safety poll of each IMAP source every
  ``imap_poll_seconds`` and a retry job for each source every 60 seconds.
- **One worker.** It takes jobs one at a time. A poll runs the one-shot
  ``run_ingest`` for its source, which reads what is new from the stored
  cursor and advances it under the cursor rule; a retry job runs the same
  retry pass ``ingest`` runs after its read. Only this thread touches item
  state or the Google API, so locking and the cursor work as in ``ingest``.
- **The health loop.** It runs the watchdog: if a job has been busy on one
  step (an item, or a listing) for longer than ``item_timeout_seconds``, the
  process exits with ``WATCHDOG_EXIT_CODE``, because a thread blocked in a
  synchronous call can't be stopped from outside. It also writes the
  heartbeat file while the worker is alive and within that time
  (``receipt_index.health``).
- **The ping thread**, when ``ingest.health.ping_url`` is set: the dead-man
  ping, on a thread of its own so a slow monitor or database never delays
  the watchdog or the heartbeat.

A transient or config failure has already written its uncounted ``failed``
row when the pipeline reports the source stopped, so the worker pauses the
source (30 seconds, doubling to 15 minutes) and the retry job brings the item
back when it is due. Shutdown therefore has nothing to write for a pause: on
SIGTERM the watchers and the timer stop at once, and the worker stops after
the item in hand, between items, where the cursor rule holds.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from imapclient.exceptions import LoginError

from receipt_index.adapters.imap import connect_imap
from receipt_index.config import GdriveSourceConfig, ImapSourceConfig
from receipt_index.fetching import SourcePool
from receipt_index.health import (
    HEARTBEAT_WRITE_SECONDS,
    Pause,
    PingLoop,
    WorkerState,
    write_heartbeat,
)
from receipt_index.locks import SourceLockLostError
from receipt_index.pipeline import run_due_retries, run_ingest
from receipt_index.repository import select_due

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    import psycopg
    from imapclient import IMAPClient
    from pydantic_ai import Agent

    from receipt_index.adapters.base import SourceAdapter
    from receipt_index.config import SourceConfig
    from receipt_index.locks import SourceLock
    from receipt_index.models import ReceiptMetadata
    from receipt_index.retries import RetryPolicy
    from receipt_index.store import EvidenceStore

logger = logging.getLogger(__name__)

# The exit code of a watchdog exit; `ingest` uses 3 for a held source lock.
WATCHDOG_EXIT_CODE = 4
# How often each source's due retries are run. The design names no key.
RETRY_PASS_SECONDS = 60.0
# A source whose job stopped (a transient or config failure, a read error)
# is left alone this long, doubling with each stop in a row.
PAUSE_FIRST_SECONDS = 30.0
PAUSE_MAX_SECONDS = 900.0
# How often the health loop runs its checks.
HEALTH_INTERVAL_SECONDS = 10.0
# How long shutdown waits for the watchers, the timer and the health loop.
JOIN_SECONDS = 30.0

# IDLE watcher timings (design section 2).
IDLE_CHECK_SLICE_SECONDS = 1.0  # how soon a watcher sees a stop
RECONNECT_FIRST_SECONDS = 5.0
RECONNECT_MAX_SECONDS = 300.0
AUTH_RETRY_SECONDS = 900.0
# Instant empty IDLE checks in a row that mean the connection has closed.
_FAST_EMPTY_LIMIT = 3
# TCP keepalive on the IDLE socket: a peer that vanished (a NAT that dropped
# the flow) is noticed after about 2 minutes, not Linux's default 2 hours.
_KEEPALIVE_IDLE_SECONDS = 60
_KEEPALIVE_INTERVAL_SECONDS = 15
_KEEPALIVE_COUNT = 4

JobKind = Literal["poll", "retry"]


@dataclass(frozen=True, order=True)
class Job:
    """One unit of the worker's work: poll a source, or run its retries."""

    kind: JobKind
    source: str


class JobQueue:
    """A FIFO of jobs in which a job already waiting is not queued twice.

    A burst of IDLE wake-ups during a long item becomes one poll.
    """

    def __init__(self) -> None:
        self._jobs: deque[Job] = deque()
        self._waiting: set[Job] = set()
        self._cond = threading.Condition()

    def put(self, job: Job) -> None:
        with self._cond:
            if job in self._waiting:
                return
            self._waiting.add(job)
            self._jobs.append(job)
            self._cond.notify()

    def get(self, timeout: float) -> Job | None:
        """The next job, or None after ``timeout`` seconds with none."""
        with self._cond:
            if not self._jobs:
                self._cond.wait(timeout)
            if not self._jobs:
                return None
            job = self._jobs.popleft()
            self._waiting.discard(job)
            return job


class ItemClock:
    """What the worker is busy on, and since when: the watchdog's input.

    It is the pipeline's ``RunControl``: ``running`` marks each item, and
    ``should_stop`` follows the stop event. Within a job, the time between
    items (a listing) is timed from the last item's end.
    """

    def __init__(
        self,
        *,
        stop: threading.Event | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stop = stop or threading.Event()
        self._now = now
        self._lock = threading.Lock()
        self._busy: tuple[float, str] | None = None
        self._job_label: str | None = None
        self._job_since: float | None = None

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def busy_since(self) -> tuple[float, str] | None:
        """When the current step began, and what it is; None when idle."""
        with self._lock:
            return self._busy

    def job_since(self) -> float | None:
        """When the current job began; None when idle."""
        with self._lock:
            return self._job_since

    def _mark(self, label: str | None) -> None:
        with self._lock:
            self._busy = None if label is None else (self._now(), label)

    @contextmanager
    def job(self, job: Job) -> Iterator[None]:
        self._job_label = f"{job.kind} {job.source}"
        with self._lock:
            self._job_since = self._now()
        self._mark(self._job_label)
        try:
            yield
        finally:
            self._job_label = None
            with self._lock:
                self._job_since = None
            self._mark(None)

    @contextmanager
    def running(self, source_id: str) -> Iterator[None]:
        self._mark(source_id)
        try:
            yield
        finally:
            self._mark(self._job_label)


class IdleWatcher:
    """Waits in IMAP IDLE on one source and queues a poll when mail arrives.

    The poll itself is the worker's: this thread only says when to read.
    Every (re)connect queues a catch-up poll. On a server without IDLE the
    watcher ends after its catch-up, and the timer's safety poll is the poll.
    """

    def __init__(
        self,
        config: ImapSourceConfig,
        queue: JobQueue,
        stop: threading.Event,
        *,
        reissue_seconds: float,
        connect: Callable[[ImapSourceConfig], IMAPClient] = connect_imap,
        check_slice: float = IDLE_CHECK_SLICE_SECONDS,
        backoff_first: float = RECONNECT_FIRST_SECONDS,
        backoff_max: float = RECONNECT_MAX_SECONDS,
        auth_retry_seconds: float = AUTH_RETRY_SECONDS,
    ) -> None:
        self.config = config
        self.queue = queue
        self.stop = stop
        self.reissue_seconds = reissue_seconds
        self._connect = connect
        self.check_slice = check_slice
        self.backoff_first = backoff_first
        self.backoff_max = backoff_max
        self.auth_retry_seconds = auth_retry_seconds
        self._poll = Job("poll", config.name)
        self._healthy = False

    def run(self) -> None:
        delay = self.backoff_first
        while not self.stop.is_set():
            self._healthy = False  # earned again by each session
            try:
                client = self._open()
            except LoginError as exc:
                # A refused login isn't retried in a tight loop. The worker's
                # safety polls record the error in sync_state meanwhile.
                logger.error(
                    "IMAP source %s: login refused (%s); trying again in %ds",
                    self.config.name,
                    exc,
                    self.auth_retry_seconds,
                )
                self.stop.wait(self.auth_retry_seconds)
                continue
            except Exception as exc:
                logger.warning(
                    "IMAP source %s: IDLE connection failed (%s); retrying in %.0fs",
                    self.config.name,
                    exc,
                    delay,
                )
                self.stop.wait(delay)
                delay = min(delay * 2, self.backoff_max)
                continue

            try:
                self.queue.put(self._poll)  # catch up on what came meanwhile
                if not client.has_capability("IDLE"):
                    logger.warning(
                        "IMAP source %s: the server has no IDLE; polling every "
                        "imap_poll_seconds instead",
                        self.config.name,
                    )
                    return
                self._idle(client)
            except Exception as exc:
                if self._healthy:
                    # The session worked before it dropped: start over.
                    delay = self.backoff_first
                logger.warning(
                    "IMAP source %s: IDLE dropped (%s); reconnecting in %.0fs",
                    self.config.name,
                    exc,
                    delay,
                )
                self.stop.wait(delay)
                delay = min(delay * 2, self.backoff_max)
            finally:
                _logout(client)

    def _open(self) -> IMAPClient:
        client = self._connect(self.config)
        try:
            _set_keepalive(client.socket())
            client.select_folder(self.config.folder, readonly=True)  # EXAMINE
        except Exception:
            _logout(client)
            raise
        return client

    def _idle(self, client: IMAPClient) -> None:
        """Sit in IDLE until stopped.

        Sets ``_healthy`` once the session has reported mail or completed a
        re-issue, so a later drop reconnects after the first delay again.
        """
        self._healthy = False
        client.idle()
        started = time.monotonic()
        fast_empty = 0
        while not self.stop.is_set():
            checked = time.monotonic()
            responses = client.idle_check(timeout=self.check_slice)
            if any(_is_bye(r) for r in responses):
                raise ConnectionError("the IMAP server ended the session (BYE)")
            # imapclient returns at once with nothing when the socket reads
            # EOF: a closed connection looks like a burst of instant empty
            # checks, not like a quiet server.
            if not responses and time.monotonic() - checked < self.check_slice / 2:
                fast_empty += 1
                if fast_empty >= _FAST_EMPTY_LIMIT:
                    raise ConnectionError("the IMAP server closed the connection")
                continue
            fast_empty = 0
            if any(_is_exists(r) for r in responses):
                self._leave_idle(client)
                self.queue.put(self._poll)
                client.idle()
                started = time.monotonic()
                self._healthy = True
            elif time.monotonic() - started >= self.reissue_seconds:
                if self._leave_idle(client):
                    self.queue.put(self._poll)
                client.idle()
                started = time.monotonic()
                self._healthy = True
        if self._leave_idle(client):
            self.queue.put(self._poll)

    @staticmethod
    def _leave_idle(client: IMAPClient) -> bool:
        """End IDLE. Return whether the server reported new mail meanwhile."""
        done = client.idle_done()
        responses = done[1] if isinstance(done, tuple) and len(done) > 1 else []
        return any(_is_exists(r) for r in responses or [])


def _is_bye(response: Any) -> bool:
    return isinstance(response, tuple) and len(response) > 0 and response[0] == b"BYE"


def _is_exists(response: Any) -> bool:
    return (
        isinstance(response, tuple) and len(response) > 1 and response[1] == b"EXISTS"
    )


def _set_keepalive(sock: Any) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for option, value in (
        ("TCP_KEEPIDLE", _KEEPALIVE_IDLE_SECONDS),
        ("TCP_KEEPINTVL", _KEEPALIVE_INTERVAL_SECONDS),
        ("TCP_KEEPCNT", _KEEPALIVE_COUNT),
    ):
        if hasattr(socket, option):  # Linux; other platforms keep defaults
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)


def _logout(client: Any) -> None:
    try:
        client.logout()
    except Exception:
        logger.debug("Error during IMAP logout", exc_info=True)


class Timer:
    """Queues each source's polls and retry jobs at their intervals.

    The first round is queued at once: a catch-up poll and a retry pass for
    every source. It only queues, so it never blocks on I/O.
    """

    def __init__(
        self,
        sources: Sequence[SourceConfig],
        queue: JobQueue,
        stop: threading.Event,
        *,
        imap_poll_seconds: float,
        drive_poll_seconds: float,
        retry_seconds: float = RETRY_PASS_SECONDS,
    ) -> None:
        self.queue = queue
        self.stop = stop
        self._schedule: list[tuple[float, Job]] = []
        for source in sources:
            poll = (
                imap_poll_seconds
                if isinstance(source, ImapSourceConfig)
                else drive_poll_seconds
            )
            self._schedule.append((poll, Job("poll", source.name)))
            self._schedule.append((retry_seconds, Job("retry", source.name)))

    def run(self) -> None:
        now = time.monotonic()
        due = [now for _ in self._schedule]
        while not self.stop.is_set():
            now = time.monotonic()
            for i, (interval, job) in enumerate(self._schedule):
                if now >= due[i]:
                    self.queue.put(job)
                    due[i] = now + interval
            wait = min(due) - time.monotonic() if due else 1.0
            self.stop.wait(max(0.0, min(wait, 1.0)))


class Worker:
    """Runs jobs one at a time through the existing ingest and retry paths.

    A job for a paused source is dropped, not deferred: the timer queues the
    source's next poll or retry job at its interval, and that one runs once
    the pause has passed. A wake-up from IDLE during a pause is therefore
    picked up by the next safety poll.
    """

    def __init__(
        self,
        *,
        sources: Sequence[SourceConfig],
        locks: Mapping[str, SourceLock],
        queue: JobQueue,
        stop: threading.Event,
        clock: ItemClock,
        open_conn: Callable[[], psycopg.Connection[dict[str, Any]]],
        adapter_factory: Callable[[SourceConfig], SourceAdapter],
        agents: Mapping[str, Agent[None, ReceiptMetadata]],
        store: EvidenceStore,
        llm_model: str,
        policy: RetryPolicy,
        configured: list[str],
        all_sources: Sequence[SourceConfig] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sources = {s.name: s for s in sources}
        self.locks = dict(locks)
        self.queue = queue
        self.stop = stop
        self.clock = clock
        self._open_conn = open_conn
        self._adapter_factory = adapter_factory
        self.agents = agents
        self.store = store
        self.llm_model = llm_model
        self.policy = policy
        self.configured = configured
        # The retry pass may look an item up in any configured source.
        self.all_sources = list(all_sources or sources)
        self._now = now
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        # Read by the health loop: every change takes _state_lock.
        self._state_lock = threading.Lock()
        # until, last delay, and when this run of pauses began (wall clock)
        self._pauses: dict[str, tuple[float, float, Pause]] = {}
        self.dropped: set[str] = set()

    def run(self) -> None:
        try:
            while not self.stop.is_set():
                job = self.queue.get(timeout=0.5)
                if job is None or self.stop.is_set():
                    continue
                try:
                    self.run_job(job)
                except Exception:
                    # Nothing a job does may end the only worker.
                    logger.exception("Worker: %s %s failed", job.kind, job.source)
        finally:
            self._close_conn()

    def paused_until(self, source: str) -> float | None:
        with self._state_lock:
            pause = self._pauses.get(source)
        return None if pause is None else pause[0]

    def health_state(self) -> tuple[dict[str, Pause], list[str]]:
        """The paused sources (since their first pause in a row, and why),
        and the sources dropped after losing their lock."""
        with self._state_lock:
            dropped = sorted(self.dropped)
            paused = {
                name: entry[2]
                for name, entry in self._pauses.items()
                if name not in self.dropped
            }
        return paused, dropped

    def run_job(self, job: Job) -> None:
        source = self.sources.get(job.source)
        if source is None or job.source in self.dropped:
            return
        until = self.paused_until(job.source)
        if until is not None and self._now() < until:
            logger.debug("Source %s is paused; leaving %s", job.source, job.kind)
            return

        error: str | None
        try:
            # Connecting and committing are timed too, so a database that
            # hangs there still trips the watchdog.
            with self.clock.job(job):
                conn = self._connection()
                if job.kind == "poll":
                    error = self._poll(conn, source)
                else:
                    error = self._retries(conn, source)
                conn.commit()
        except Exception as exc:
            # One bad job (a cancelled statement, a dropped connection) must
            # not end the only worker.
            logger.exception("Source %s: %s job failed", job.source, job.kind)
            error = str(exc)
            self._discard_conn()

        if error is None:
            with self._state_lock:
                self._pauses.pop(job.source, None)
            return
        # The lock check and re-take reach the database too, so the watchdog
        # times them as well.
        with self.clock.job(Job(job.kind, job.source)):
            lost = self._lock_lost(job.source)
        if not lost:
            self._pause(job.source, error)

    def _poll(
        self, conn: psycopg.Connection[dict[str, Any]], source: SourceConfig
    ) -> str | None:
        result = run_ingest(
            conn=conn,
            adapter=self._adapter_factory(source),
            store=self.store,
            source_name=source.name,
            source_type=source.type,
            agent=self.agents[_agent_kind(source)],
            llm_model=self.llm_model,
            source_lock=self.locks[source.name],
            policy=self.policy,
            control=self.clock,
        )
        if result.processed or result.skipped or result.failed:
            logger.info(
                "Source %s: processed %d, skipped %d, failed %d",
                source.name,
                result.processed,
                result.skipped,
                result.failed,
            )
        return result.source_error

    def _retries(
        self, conn: psycopg.Connection[dict[str, Any]], source: SourceConfig
    ) -> str | None:
        due = select_due(
            conn,
            source_name=source.name,
            source_type=source.type,
            configured=self.configured,
        )
        if not due:
            return None
        # A pool per job: a source it marks unavailable, and the adapter
        # connections it opens, last one pass, as in `ingest`.
        with ExitStack() as stack:
            pool = SourcePool(self.all_sources, self._adapter_factory, stack)
            retried = run_due_retries(
                conn,
                due,
                pool=pool,
                store=self.store,
                agents=self.agents,
                llm_model=self.llm_model,
                policy=self.policy,
                own_source=source.name,
                control=self.clock,
            )
        return None if retried.stopped is None else f"retries {retried.stopped}"

    def _lock_lost(self, name: str) -> bool:
        """After a failed job: re-take a lost source lock, or drop the source.

        Returns whether the lock was lost (the job's error is then the loss).
        """
        lock = self.locks[name]
        try:
            lock.check()
            return False
        except SourceLockLostError:
            pass
        lock.release()
        try:
            taken = lock.acquire()
        except Exception as exc:
            # The database is down: the next job after the pause tries again.
            logger.warning(
                "Source %s: can't re-take its source lock yet (%s)", name, exc
            )
            self._pause(name, "source lock lost; database unreachable")
            return True
        if taken:
            logger.warning("Source %s: re-took its lost source lock", name)
        else:
            logger.error(
                "Source %s: lost its source lock to another process; no longer "
                "watching it",
                name,
            )
            with self._state_lock:
                self.dropped.add(name)
        return True

    def _pause(self, name: str, error: str) -> None:
        with self._state_lock:
            last = self._pauses.get(name)
            if last is None:
                delay = PAUSE_FIRST_SECONDS
                since = Pause(since=datetime.now(UTC), reason=error)
            else:
                delay = min(last[1] * 2, PAUSE_MAX_SECONDS)
                since = Pause(since=last[2].since, reason=error)
            self._pauses[name] = (self._now() + delay, delay, since)
        logger.warning("Source %s paused for %.0fs: %s", name, delay, error)

    def _connection(self) -> psycopg.Connection[dict[str, Any]]:
        conn = self._conn
        if conn is None or conn.closed or conn.broken:
            self._discard_conn()
            conn = self._conn = self._open_conn()
        return conn

    def _discard_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            logger.debug("Error closing the worker connection", exc_info=True)

    def _close_conn(self) -> None:
        self._discard_conn()


def _agent_kind(source: SourceConfig) -> str:
    return "document" if isinstance(source, GdriveSourceConfig) else "email"


class HealthLoop:
    """The health loop: the watchdog, and the heartbeat file.

    It keeps watching until the worker has finished, so a stop during a hung
    item still ends in a watchdog exit rather than a process that never ends.
    The heartbeat is written every ``heartbeat_seconds`` while the worker is
    alive and idle, or busy on its current step for no longer than
    ``item_timeout_seconds``: a slow item keeps it fresh, a wedged one doesn't.
    """

    def __init__(
        self,
        clock: ItemClock,
        *,
        stop: threading.Event,
        worker_done: threading.Event,
        item_timeout_seconds: float,
        exit_fn: Callable[[int], object] = os._exit,
        now: Callable[[], float] = time.monotonic,
        interval_seconds: float = HEALTH_INTERVAL_SECONDS,
        heartbeat_path: Path | None = None,
        state: Callable[[], WorkerState] | None = None,
        heartbeat_seconds: float = HEARTBEAT_WRITE_SECONDS,
    ) -> None:
        self.clock = clock
        self.stop = stop
        self.worker_done = worker_done
        self.item_timeout_seconds = item_timeout_seconds
        self._exit = exit_fn
        self._now = now
        self.interval_seconds = interval_seconds
        self.heartbeat_path = heartbeat_path
        self._state = state
        self.heartbeat_seconds = heartbeat_seconds
        self._last_beat: float | None = None
        self._write_failing = False

    def run(self) -> None:
        while not self.worker_done.is_set():
            self.check()
            self.worker_done.wait(self.interval_seconds)

    def check(self) -> None:
        running = self._watchdog()
        self._beat(running)

    def beat_now(self) -> None:
        """Write the heartbeat at once if the worker is fit (``serve``'s start)."""
        self._last_beat = None
        busy = self.clock.busy_since()
        self._beat(None if busy is None else self._now() - busy[0])

    def _beat(self, running: float | None) -> None:
        if self.heartbeat_path is None or self._state is None:
            return
        if self.worker_done.is_set():
            return  # a dead worker gets no heartbeat
        if running is not None and running > self.item_timeout_seconds:
            return  # nor does a wedged one
        now = self._now()
        if (
            self._last_beat is not None
            and now - self._last_beat < self.heartbeat_seconds
        ):
            return
        try:
            write_heartbeat(self.heartbeat_path, self._state())
        except Exception as exc:
            # Never let a full /tmp end the loop, and the watchdog with it.
            # Logged once per change, not every tick.
            if not self._write_failing:
                logger.warning(
                    "Health: can't write the heartbeat (%s)", type(exc).__name__
                )
            self._write_failing = True
            return
        if self._write_failing:
            logger.info("Health: heartbeat written again")
            self._write_failing = False
        self._last_beat = now

    def _watchdog(self) -> float | None:
        """Exit past ``item_timeout_seconds``; return how long the step has run."""
        busy = self.clock.busy_since()
        if busy is None:
            return None
        since, label = busy
        running = self._now() - since
        if running > self.item_timeout_seconds:
            logger.critical(
                "Watchdog: %s has run %.0fs, past item_timeout_seconds (%d); "
                "exiting so the container restarts",
                label,
                running,
                self.item_timeout_seconds,
            )
            # No logging.shutdown(): the stuck thread may hold a handler's
            # lock. The stream handler has already written the line.
            self._exit(WATCHDOG_EXIT_CODE)
        return running


@dataclass
class ServeTimings:
    """The intervals ``serve`` runs on, from the ``ingest:`` block."""

    idle_reissue_seconds: float
    imap_poll_seconds: float
    drive_poll_seconds: float
    item_timeout_seconds: float
    retry_seconds: float = RETRY_PASS_SECONDS
    join_seconds: float = JOIN_SECONDS


class Service:
    """Starts the threads, and stops them on ``stop()``.

    ``locks`` holds a source lock for each source to watch; the caller takes
    them (and releases them after ``run``). Each source in ``sources`` must
    have one.
    """

    def __init__(
        self,
        *,
        sources: Sequence[SourceConfig],
        all_sources: Sequence[SourceConfig],
        locks: Mapping[str, SourceLock],
        timings: ServeTimings,
        open_conn: Callable[[], psycopg.Connection[dict[str, Any]]],
        adapter_factory: Callable[[SourceConfig], SourceAdapter],
        agents: Mapping[str, Agent[None, ReceiptMetadata]],
        store: EvidenceStore,
        llm_model: str,
        policy: RetryPolicy,
        imap_connect: Callable[[ImapSourceConfig], IMAPClient] = connect_imap,
        exit_fn: Callable[[int], object] = os._exit,
        heartbeat_path: Path | None = None,
        ping_url: str | None = None,
        ping_interval_seconds: float = 300.0,
        ping_probe: Callable[[], list[str]] | None = None,
        requested: Sequence[str] | None = None,
    ) -> None:
        self.timings = timings
        self._watching = [s.name for s in sources]
        # What serve was asked to watch, held or not (`status` flags the gap).
        self._requested = list(requested) if requested is not None else self._watching
        self._started_at = datetime.now(UTC)
        self.stop_event = threading.Event()
        self.worker_done = threading.Event()
        self.queue = JobQueue()
        self.clock = ItemClock(stop=self.stop_event)
        self.worker = Worker(
            sources=sources,
            all_sources=all_sources,
            locks=locks,
            queue=self.queue,
            stop=self.stop_event,
            clock=self.clock,
            open_conn=open_conn,
            adapter_factory=adapter_factory,
            agents=agents,
            store=store,
            llm_model=llm_model,
            policy=policy,
            configured=[s.name for s in all_sources],
        )
        self.watchers = [
            IdleWatcher(
                s,
                self.queue,
                self.stop_event,
                reissue_seconds=timings.idle_reissue_seconds,
                connect=imap_connect,
            )
            for s in sources
            if isinstance(s, ImapSourceConfig)
        ]
        self.timer = Timer(
            sources,
            self.queue,
            self.stop_event,
            imap_poll_seconds=timings.imap_poll_seconds,
            drive_poll_seconds=timings.drive_poll_seconds,
            retry_seconds=timings.retry_seconds,
        )
        self.health = HealthLoop(
            self.clock,
            stop=self.stop_event,
            worker_done=self.worker_done,
            item_timeout_seconds=timings.item_timeout_seconds,
            exit_fn=exit_fn,
            heartbeat_path=heartbeat_path,
            state=self.worker_state,
        )
        self.ping: PingLoop | None = None
        if ping_url and ping_probe is not None:
            self.ping = PingLoop(
                ping_url,
                probe=ping_probe,
                stop=self.stop_event,
                interval_seconds=ping_interval_seconds,
            )
        self._threads: list[threading.Thread] = []
        self._worker_thread: threading.Thread | None = None
        self.worker_failed = False
        self._stop_requested = False

    def start(self) -> None:
        def work() -> None:
            try:
                self.worker.run()
            except BaseException:
                logger.critical("serve: the worker crashed", exc_info=True)
            finally:
                self.worker_done.set()
                if not self.stop_event.is_set():
                    # A serve with no worker ingests nothing: stop, so the
                    # process exits non-zero and its supervisor restarts it.
                    logger.critical("serve: the worker ended; stopping")
                    self.worker_failed = True
                    self.stop()

        self._worker_thread = self._spawn(work, "worker")
        # The first ping reads this run's heartbeat, not one a previous
        # process left in /tmp.
        self.health.beat_now()
        self._spawn(self.health.run, "health")
        if self.ping is not None:
            self._spawn(self.ping.run, "ping")
        self._spawn(self.timer.run, "timer")
        for watcher in self.watchers:
            self._spawn(watcher.run, f"idle-{watcher.config.name}")

    def worker_state(self) -> WorkerState:
        """What the heartbeat file says about this process."""
        paused, lost = self.worker.health_state()
        busy = self.clock.busy_since()
        job = self.clock.job_since()
        return WorkerState(
            pid=os.getpid(),
            written_at=datetime.now(UTC),
            watching=[n for n in self._watching if n not in lost],
            paused=paused,
            lock_lost=lost,
            busy_seconds=None if busy is None else time.monotonic() - busy[0],
            job_seconds=None if job is None else time.monotonic() - job,
            requested=list(self._requested),
            started_at=self._started_at,
        )

    def _spawn(self, target: Callable[[], None], name: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=f"serve-{name}", daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    def stop(self) -> None:
        """Ask every thread to stop. Not for a signal handler: use ``request_stop``."""
        self.stop_event.set()

    def request_stop(self) -> None:
        """Ask for a stop from a signal handler.

        It only sets a flag: ``Event.set`` takes a lock the interrupted main
        thread may already hold inside ``Event.wait``. ``wait`` turns the
        flag into a ``stop()``.
        """
        self._stop_requested = True

    def wait(self, poll_seconds: float = 1.0) -> None:
        """Block until a stop is asked for, waking often to see the flag."""
        while not self._stop_requested and not self.stop_event.wait(poll_seconds):
            pass
        if self._stop_requested:
            logger.info("serve: stop requested; stopping")
        self.stop()

    def shutdown(self) -> bool:
        """Stop and join the threads. Return whether the worker finished cleanly.

        The worker gets until the item in hand is done: at most
        ``item_timeout_seconds``, after which the watchdog exits the process.
        """
        self.stop()
        worker = self._worker_thread
        if worker is not None:
            worker.join(self.timings.item_timeout_seconds + self.timings.join_seconds)
        deadline = time.monotonic() + self.timings.join_seconds
        for thread in self._threads:
            if thread is not worker:
                thread.join(max(0.0, deadline - time.monotonic()))
        finished = worker is None or not worker.is_alive()
        if not finished:
            logger.error("serve: the worker didn't stop in time")
        return finished and not self.worker_failed
