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
- **The health loop.** For now it runs only the watchdog: if a job has been
  busy on one step (an item, or a listing) for longer than
  ``item_timeout_seconds``, the process exits with ``WATCHDOG_EXIT_CODE``,
  because a thread blocked in a synchronous call can't be stopped from
  outside. The health ticket adds the heartbeat and the dead-man ping here.

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
from typing import TYPE_CHECKING, Any, Literal

from imapclient.exceptions import LoginError

from receipt_index.adapters.imap import connect_imap
from receipt_index.config import GdriveSourceConfig, ImapSourceConfig
from receipt_index.fetching import SourcePool
from receipt_index.locks import SourceLockLostError
from receipt_index.pipeline import run_due_retries, run_ingest
from receipt_index.repository import select_due

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

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

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def busy_since(self) -> tuple[float, str] | None:
        """When the current step began, and what it is; None when idle."""
        with self._lock:
            return self._busy

    def _mark(self, label: str | None) -> None:
        with self._lock:
            self._busy = None if label is None else (self._now(), label)

    @contextmanager
    def job(self, job: Job) -> Iterator[None]:
        self._job_label = f"{job.kind} {job.source}"
        self._mark(self._job_label)
        try:
            yield
        finally:
            self._job_label = None
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

    def run(self) -> None:
        delay = self.backoff_first
        while not self.stop.is_set():
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
                if self._idle(client):
                    delay = self.backoff_first
            except Exception as exc:
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
            sock = client.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            client.select_folder(self.config.folder, readonly=True)  # EXAMINE
        except Exception:
            _logout(client)
            raise
        return client

    def _idle(self, client: IMAPClient) -> bool:
        """Sit in IDLE until stopped. Return whether a full cycle completed."""
        healthy = False
        client.idle()
        started = time.monotonic()
        while not self.stop.is_set():
            responses = client.idle_check(timeout=self.check_slice)
            if any(_is_exists(r) for r in responses):
                client.idle_done()
                self.queue.put(self._poll)
                client.idle()
                started = time.monotonic()
            elif time.monotonic() - started >= self.reissue_seconds:
                client.idle_done()
                client.idle()
                started = time.monotonic()
                healthy = True
        client.idle_done()
        return healthy


def _is_exists(response: Any) -> bool:
    return (
        isinstance(response, tuple) and len(response) > 1 and response[1] == b"EXISTS"
    )


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
    """Runs jobs one at a time through the existing ingest and retry paths."""

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
        self._pauses: dict[str, tuple[float, float]] = {}  # until, last delay
        self.dropped: set[str] = set()

    def run(self) -> None:
        try:
            while not self.stop.is_set():
                job = self.queue.get(timeout=0.5)
                if job is None or self.stop.is_set():
                    continue
                self.run_job(job)
        finally:
            self._close_conn()

    def paused_until(self, source: str) -> float | None:
        pause = self._pauses.get(source)
        return None if pause is None else pause[0]

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
            conn = self._connection()
            with self.clock.job(job):
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
            self._pauses.pop(job.source, None)
            return
        if self._lock_lost(job.source):
            return
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
        if lock.acquire():
            logger.warning("Source %s: re-took its lost source lock", name)
        else:
            logger.error(
                "Source %s: lost its source lock to another process; no longer "
                "watching it",
                name,
            )
            self.dropped.add(name)
        return True

    def _pause(self, name: str, error: str) -> None:
        last = self._pauses.get(name)
        delay = (
            PAUSE_FIRST_SECONDS if last is None else min(last[1] * 2, PAUSE_MAX_SECONDS)
        )
        self._pauses[name] = (self._now() + delay, delay)
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
    """The health loop. This ticket gives it only the watchdog.

    It keeps watching until the worker has finished, so a stop during a hung
    item still ends in a watchdog exit rather than a process that never ends.
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
    ) -> None:
        self.clock = clock
        self.stop = stop
        self.worker_done = worker_done
        self.item_timeout_seconds = item_timeout_seconds
        self._exit = exit_fn
        self._now = now
        self.interval_seconds = interval_seconds

    def run(self) -> None:
        while not self.worker_done.is_set():
            self.check()
            self.worker_done.wait(self.interval_seconds)

    def check(self) -> None:
        busy = self.clock.busy_since()
        if busy is None:
            return
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
            logging.shutdown()
            self._exit(WATCHDOG_EXIT_CODE)


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
    ) -> None:
        self.timings = timings
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
        )
        self._threads: list[threading.Thread] = []
        self._worker_thread: threading.Thread | None = None

    def start(self) -> None:
        def work() -> None:
            try:
                self.worker.run()
            finally:
                self.worker_done.set()

        self._worker_thread = self._spawn(work, "worker")
        self._spawn(self.health.run, "health")
        self._spawn(self.timer.run, "timer")
        for watcher in self.watchers:
            self._spawn(watcher.run, f"idle-{watcher.config.name}")

    def _spawn(self, target: Callable[[], None], name: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=f"serve-{name}", daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    def stop(self) -> None:
        """Ask every thread to stop. Safe to call from a signal handler."""
        self.stop_event.set()

    def wait(self, poll_seconds: float = 1.0) -> None:
        """Block until ``stop()``, waking often so signals are handled."""
        while not self.stop_event.wait(poll_seconds):
            pass

    def shutdown(self) -> bool:
        """Stop and join the threads. Return whether the worker finished.

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
        return finished
