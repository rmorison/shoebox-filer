"""Health for ``serve``: the heartbeat, the checks and the dead-man ping.

Always-on ingest design, section 9.

- **The heartbeat file.** ``serve``'s health loop rewrites it every 30
  seconds while the worker is alive and within ``item_timeout_seconds``. Its
  age is the liveness signal. Its content tells ``status``, a separate
  process, what lives only in ``serve``'s memory: the sources it watches,
  the sources paused on errors and since when, and the sources whose lock it
  lost.
- **The checks** (`status --check`, and each ping round): a stale or missing
  heartbeat, a source not read successfully within its stale threshold, a
  lost source lock, a pause longer than the pause threshold, and an
  unreachable database. Each problem is one data-light line: the check's
  name, the source name, and an age or a count, never error text.
- **The ping** runs on its own thread in ``serve``. A round with no problems
  and no item turned terminal within ``terminal_alert_seconds`` sends a GET
  to ``ping_url``; any other round POSTs the problem lines to
  ``ping_url/fail``. The URL is a capability: it is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from receipt_index.config import ImapSourceConfig

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Sequence

    import psycopg

    from receipt_index.config import AppConfig, IngestConfig, SourceConfig
    from receipt_index.models import SyncState

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = Path("/tmp/receipt-index-serve.heartbeat")
# How often the health loop rewrites the heartbeat file.
HEARTBEAT_WRITE_SECONDS = 30.0
# Each socket operation of a ping (connect, send, read) gets this long.
PING_TIMEOUT_SECONDS = 10.0
# `status` counts skipped items over this window.
SKIPPED_WINDOW = timedelta(days=7)
# A poll's stale threshold, as a multiple of its interval, by default.
STALE_POLLS = 3
# `status` and each ping round read the database on a short leash: Docker's
# healthcheck gives the whole command 30 seconds by default.
STATUS_CONNECT_TIMEOUT_SECONDS = 5
STATUS_STATEMENT_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# The heartbeat file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pause:
    """A source the worker has left alone after errors, since ``since``."""

    since: datetime
    reason: str


@dataclass(frozen=True)
class WorkerState:
    """What ``serve`` writes into the heartbeat file."""

    pid: int
    written_at: datetime
    watching: list[str] = field(default_factory=list)
    paused: dict[str, Pause] = field(default_factory=dict)
    lock_lost: list[str] = field(default_factory=list)
    # How long the worker had been on its current step; None when idle.
    busy_seconds: float | None = None
    # The sources this serve was asked to watch (every configured source, or
    # the --source set), and when it started.
    requested: list[str] = field(default_factory=list)
    started_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "written_at": self.written_at.isoformat(),
            "watching": list(self.watching),
            "paused": {
                name: {"since": p.since.isoformat(), "reason": p.reason}
                for name, p in self.paused.items()
            },
            "lock_lost": list(self.lock_lost),
            "busy_seconds": self.busy_seconds,
            "requested": list(self.requested),
            "started_at": None
            if self.started_at is None
            else self.started_at.isoformat(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> WorkerState:
        return cls(
            pid=int(data["pid"]),
            written_at=datetime.fromisoformat(data["written_at"]),
            watching=[str(n) for n in data.get("watching", [])],
            paused={
                str(name): Pause(
                    since=datetime.fromisoformat(p["since"]),
                    reason=str(p.get("reason", "")),
                )
                for name, p in data.get("paused", {}).items()
            },
            lock_lost=[str(n) for n in data.get("lock_lost", [])],
            busy_seconds=(
                None
                if data.get("busy_seconds") is None
                else float(data["busy_seconds"])
            ),
            requested=[str(n) for n in data.get("requested", [])],
            started_at=(
                None
                if data.get("started_at") is None
                else datetime.fromisoformat(data["started_at"])
            ),
        )


@dataclass(frozen=True)
class Heartbeat:
    """The heartbeat file as read: its age, and its content if it parses.

    ``ours`` is False when another user owns the file: on a host whose /tmp
    is shared, anyone could create it first and keep it fresh.
    """

    age_seconds: float
    state: WorkerState | None
    ours: bool = True


def write_heartbeat(path: Path, state: WorkerState) -> None:
    """Replace the heartbeat file in one step, so a reader never sees half."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state.to_json(), f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_heartbeat(path: Path, *, now: float | None = None) -> Heartbeat | None:
    """The heartbeat file's age and content, or None if there is none.

    A file that exists but doesn't parse still has an age: the age is the
    liveness signal, the content is detail.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    age = max(0.0, (time.time() if now is None else now) - stat.st_mtime)
    if stat.st_uid != os.getuid():
        return Heartbeat(age_seconds=age, state=None, ours=False)
    state: WorkerState | None
    try:
        state = WorkerState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        state = None
    return Heartbeat(age_seconds=age, state=state)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def source_folder(source: SourceConfig) -> str:
    """The folder a source's sync-state row is keyed on (as its adapter's)."""
    return source.folder if isinstance(source, ImapSourceConfig) else source.folder_id


def stale_threshold(source: SourceConfig, ingest: IngestConfig) -> int:
    """How old a source's last successful read may be before it's stale."""
    explicit = ingest.health.source_stale_seconds
    if explicit is not None:
        return explicit
    return STALE_POLLS * ingest.poll_seconds(source.type)


def pause_threshold(sources: Sequence[SourceConfig], ingest: IngestConfig) -> int:
    """How long a source may stay paused before it's a problem."""
    explicit = ingest.health.source_stale_seconds
    if explicit is not None:
        return explicit
    return max((stale_threshold(s, ingest) for s in sources), default=0)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """One failed check. ``line`` is all a ping body ever carries."""

    check: str
    detail: str
    source: str | None = None

    @property
    def line(self) -> str:
        parts = [self.check, self.source, self.detail]
        return " ".join(p for p in parts if p)


@dataclass
class SourceStatus:
    """A configured source and its sync-state row, if it has one."""

    name: str
    type: str
    folder: str
    stale_after_seconds: int
    row: SyncState | None

    def success_age(self, now: datetime) -> float | None:
        if self.row is None or self.row.last_success_at is None:
            return None
        return max(0.0, (now - self.row.last_success_at).total_seconds())


@dataclass
class Report:
    """Everything ``status`` shows.

    When the database was down, ``database_ok`` is False, ``retries`` is
    None and no source has a row.
    """

    now: datetime
    sources: list[SourceStatus]
    heartbeat: Heartbeat | None
    database_ok: bool
    orphans: list[SyncState] = field(default_factory=list)
    retries: dict[str, int] | None = None
    terminal_recent: int = 0
    problems: list[Problem] = field(default_factory=list)


def _secs(seconds: float) -> str:
    return f"{int(seconds)}s"


def _polled_since(status: SourceStatus, started: datetime) -> bool:
    """Whether the source was read (successfully or not) since ``started``.

    The start-up grace ends at the first read, so a serve that restarts
    again and again can't keep a failing source inside it.
    """
    row = status.row
    return (
        row is not None and row.last_poll_at is not None and row.last_poll_at >= started
    )


def evaluate(
    *,
    sources: list[SourceStatus],
    heartbeat: Heartbeat | None,
    database_ok: bool,
    ingest: IngestConfig,
    pause_after: int,
    now: datetime,
) -> list[Problem]:
    """The ``status --check`` conditions (design section 9), one per problem."""
    problems: list[Problem] = []
    health = ingest.health
    fresh = (
        heartbeat is not None
        and heartbeat.ours
        and heartbeat.age_seconds <= health.heartbeat_stale_seconds
    )
    if heartbeat is None:
        problems.append(Problem("heartbeat", "missing"))
    elif not heartbeat.ours:
        problems.append(Problem("heartbeat", "not ours"))
    elif not fresh:
        problems.append(Problem("heartbeat", f"stale {_secs(heartbeat.age_seconds)}"))
    # A stale heartbeat's content belongs to a process that may be gone.
    state = heartbeat.state if fresh and heartbeat is not None else None

    # One worker takes the sources in turn, so a long step holds the others
    # back: each threshold is extended by the current step's running time,
    # capped at item_timeout_seconds (the watchdog's limit). Past that cap,
    # a source or a pause is a problem however busy the worker is, and
    # whatever the start-up grace below says.
    busy_for = (
        0.0 if state is None or state.busy_seconds is None else state.busy_seconds
    )
    allowance = min(busy_for, float(ingest.item_timeout_seconds))
    backstop = float(ingest.item_timeout_seconds)
    # A serve that just started hasn't read its sources yet: each source's
    # age counts from the later of its last success and the start, so a
    # restart after downtime gets one stale threshold to catch up.
    started = state.started_at if state is not None else None

    if not database_ok:
        problems.append(Problem("database", "unreachable"))
    else:
        watched = set(state.watching) if state is not None else None
        for status in sources:
            if watched is not None and status.name not in watched:
                continue  # left to whatever else reads it (`serve --source`)
            age = status.success_age(now)
            if age is not None and age > status.stale_after_seconds + backstop:
                problems.append(Problem("source", f"stale {_secs(age)}", status.name))
                continue
            since_start = None if started is None else (now - started).total_seconds()
            if (
                since_start is not None
                and started is not None
                and (age is None or since_start < age)
                and since_start <= status.stale_after_seconds
                and not _polled_since(status, started)
            ):
                continue  # this serve hasn't read it yet: catching up
            if age is None:
                problems.append(Problem("source", "never succeeded", status.name))
            elif age > status.stale_after_seconds + allowance:
                problems.append(Problem("source", f"stale {_secs(age)}", status.name))

    if state is not None:
        lost = set(state.lock_lost)
        for name in sorted(lost):
            problems.append(Problem("source", "lock lost", name))
        held = set(state.watching)
        for name in sorted(set(state.requested) - held - lost):
            # Another process held its lock when serve started, and serve
            # never takes it later: nothing watches this source.
            problems.append(Problem("source", "not held", name))
    if state is not None:
        for name, pause in sorted(state.paused.items()):
            paused_for = (now - pause.since).total_seconds()
            if paused_for > pause_after + allowance:
                problems.append(Problem("source", f"paused {_secs(paused_for)}", name))
    return problems


def collect(
    config: AppConfig,
    open_conn: Callable[[], psycopg.Connection[dict[str, Any]]],
    *,
    heartbeat_path: Path = HEARTBEAT_PATH,
    now: datetime | None = None,
    retries: bool = True,
) -> Report:
    """Read the heartbeat file and the database, and run the checks.

    ``retries`` adds the retry counts ``status`` shows; the ping needs only
    the count of items turned terminal within ``terminal_alert_seconds``.
    A database that can't be reached is a problem, not an exception.
    """
    from receipt_index.repository import (
        count_due_retries,
        count_ingest_failures,
        list_sync_state,
    )

    now = now or datetime.now(UTC)
    ingest = config.ingest
    heartbeat = read_heartbeat(heartbeat_path, now=now.timestamp())
    statuses = [
        SourceStatus(
            name=s.name,
            type=s.type,
            folder=source_folder(s),
            stale_after_seconds=stale_threshold(s, ingest),
            row=None,
        )
        for s in config.sources
    ]
    report = Report(now=now, sources=statuses, heartbeat=heartbeat, database_ok=True)

    window = ingest.health.terminal_alert_seconds
    try:
        with open_conn() as conn:
            rows = list_sync_state(conn)
            if retries:
                pending = count_due_retries(conn)
                report.retries = {
                    "pending": pending,
                    # Due later: `failures --status retrying` counts both.
                    # Two statements: an item can clear between them.
                    "retrying": max(
                        0, count_ingest_failures(conn, status="retrying") - pending
                    ),
                    "queued": count_ingest_failures(conn, status="queued"),
                    "terminal": count_ingest_failures(conn, status="terminal"),
                    "skipped_7d": count_ingest_failures(
                        conn, status="skipped", since=now - SKIPPED_WINDOW
                    ),
                }
            if window > 0:
                report.terminal_recent = count_ingest_failures(
                    conn, status="terminal", since=now - timedelta(seconds=window)
                )
    except Exception as exc:
        # No exc_info: the line must not grow a traceback on every
        # healthcheck. The class is enough to tell a timeout from a refusal.
        logger.warning("health: the database can't be reached (%s)", type(exc).__name__)
        report.database_ok = False
        rows = []

    by_key = {(r.source_name, r.folder): r for r in rows}
    for status in statuses:
        status.row = by_key.pop((status.name, status.folder), None)
    report.orphans = list(by_key.values())
    report.problems = evaluate(
        sources=statuses,
        heartbeat=heartbeat,
        database_ok=report.database_ok,
        ingest=ingest,
        pause_after=pause_threshold(config.sources, ingest),
        now=now,
    )
    return report


def window_label(seconds: int) -> str:
    """``24h`` for a whole number of hours, else ``900s``."""
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds}s"


def ping_lines(report: Report, terminal_alert_seconds: int) -> list[str]:
    """The lines a ping round reports; empty means healthy (a GET)."""
    lines = [p.line for p in report.problems]
    if terminal_alert_seconds > 0 and report.terminal_recent > 0:
        lines.append(
            f"terminal {report.terminal_recent} in "
            f"{window_label(terminal_alert_seconds)}"
        )
    return lines


# ---------------------------------------------------------------------------
# Rendering (`status`)
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _error_current(row: SyncState) -> bool:
    """Whether ``last_error`` is newer than the last success.

    ``record_poll`` keeps the last error after later successes, so an old
    error is shown as cleared.
    """
    if row.last_error is None:
        return False
    if row.last_success_at is None or row.last_error_at is None:
        return True
    return row.last_error_at > row.last_success_at


def _cursor(row: SyncState | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if row.source_type == "imap":
        return {"uidvalidity": row.imap_uidvalidity, "last_uid": row.imap_last_uid}
    return {"page_token": row.drive_page_token}


def report_json(report: Report) -> dict[str, Any]:
    """``status --output json``."""
    sources = []
    for status in report.sources:
        row = status.row
        age = status.success_age(report.now)
        sources.append(
            {
                "name": status.name,
                "type": status.type,
                "folder": status.folder,
                "cursor": _cursor(row),
                "last_poll_at": _iso(row.last_poll_at) if row else None,
                "last_success_at": _iso(row.last_success_at) if row else None,
                "success_age_seconds": None if age is None else int(age),
                "stale_after_seconds": status.stale_after_seconds,
                "last_error": row.last_error if row else None,
                "last_error_at": _iso(row.last_error_at) if row else None,
                "last_error_current": _error_current(row) if row else False,
                "resets": row.resets if row else 0,
                "archive_enabled_at": _iso(row.archive_enabled_at) if row else None,
            }
        )
    worker: dict[str, Any] | None = None
    if report.heartbeat is not None:
        state = report.heartbeat.state
        worker = {
            "heartbeat_age_seconds": int(report.heartbeat.age_seconds),
            "pid": state.pid if state else None,
            "watching": state.watching if state else [],
            "busy_seconds": (
                None
                if state is None or state.busy_seconds is None
                else int(state.busy_seconds)
            ),
            "paused": [
                {
                    "source": name,
                    "since": _iso(p.since),
                    "seconds": int((report.now - p.since).total_seconds()),
                    "reason": p.reason,
                }
                for name, p in sorted((state.paused if state else {}).items())
            ],
            "lock_lost": state.lock_lost if state else [],
        }
    return {
        "database": "ok" if report.database_ok else "unreachable",
        "sources": sources,
        "worker": worker,
        "retries": report.retries,
        "orphans": [
            {
                "source_name": r.source_name,
                "source_type": r.source_type,
                "folder": r.folder,
            }
            for r in report.orphans
        ],
        "problems": [p.line for p in report.problems],
    }


def _ago(seconds: float | None) -> str:
    return "never" if seconds is None else f"{int(seconds)}s ago"


def report_text(report: Report) -> list[str]:
    """``status`` as text lines."""
    data = report_json(report)
    out = ["Sources"]
    for src in data["sources"]:
        out.append(f"  {src['name']} ({src['type']}, {src['folder']})")
        cursor = src["cursor"]
        if cursor is None:
            out.append("    no sync state yet")
            continue
        if src["type"] == "imap":
            out.append(
                f"    cursor: uidvalidity {cursor['uidvalidity']}, "
                f"last uid {cursor['last_uid']}"
            )
        else:
            out.append(f"    cursor: page token {cursor['page_token']}")
        out.append(f"    last poll: {src['last_poll_at'] or 'never'}")
        out.append(
            f"    last success: {src['last_success_at'] or 'never'} "
            f"({_ago(src['success_age_seconds'])}; stale after "
            f"{src['stale_after_seconds']}s)"
        )
        if src["last_error"] is not None:
            cleared = "" if src["last_error_current"] else " (cleared since)"
            out.append(
                f"    last error: {src['last_error_at']}{cleared}: {src['last_error']}"
            )
        archive = src["archive_enabled_at"] or "off"
        out.append(f"    resets: {src['resets']}; archive: {archive}")
    if report.orphans:
        out.append("Orphaned sync state (no configured source)")
        out.extend(f"  {o['source_name']} ({o['folder']})" for o in data["orphans"])
    out.append("Worker")
    worker = data["worker"]
    if worker is None:
        out.append("  heartbeat: none (serve isn't running here)")
    else:
        out.append(
            f"  heartbeat: {worker['heartbeat_age_seconds']}s ago (pid {worker['pid']})"
        )
        if worker["watching"]:
            out.append(f"  watching: {', '.join(worker['watching'])}")
        if worker["busy_seconds"] is not None:
            out.append(f"  busy: {worker['busy_seconds']}s on the current step")
        for pause in worker["paused"]:
            out.append(
                f"  paused: {pause['source']} for {pause['seconds']}s: "
                f"{pause['reason']}"
            )
        for name in worker["lock_lost"]:
            out.append(f"  lock lost: {name} (no longer watched)")
    out.append("Retries")
    retries = data["retries"]
    if retries is None:
        out.append("  unknown (database unreachable)")
    else:
        out.append(
            f"  pending {retries['pending']}, retrying {retries['retrying']}, "
            f"queued {retries['queued']}, terminal {retries['terminal']}, "
            f"skipped in the last 7 days {retries['skipped_7d']}"
        )
    out.append("Problems")
    out.extend(f"  {line}" for line in data["problems"] or ["none"])
    return out


# ---------------------------------------------------------------------------
# The dead-man ping
# ---------------------------------------------------------------------------


class PingLoop:
    """Pings the external monitor every ``interval_seconds`` until stopped.

    The first round runs at once. ``probe`` returns the problem lines; any
    exception it raises is itself a failure round. Nothing a round does is
    raised, so the thread outlives a monitor that is down, slow or gone.
    """

    def __init__(
        self,
        url: str,
        *,
        probe: Callable[[], list[str]],
        stop: threading.Event,
        interval_seconds: float,
        timeout_seconds: float = PING_TIMEOUT_SECONDS,
    ) -> None:
        self._url = url
        self._probe = probe
        self.stop = stop
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"PingLoop(interval_seconds={self.interval_seconds})"

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.ping_once()
            except Exception as exc:  # pragma: no cover - ping_once raises nothing
                logger.warning("health: ping round failed (%s)", type(exc).__name__)
            self.stop.wait(self.interval_seconds)

    def ping_once(self) -> bool:
        """Run one round. Return whether the monitor took the ping."""
        try:
            lines = self._probe()
        except Exception as exc:
            logger.warning("health: the checks failed to run (%s)", type(exc).__name__)
            lines = ["health checks failed"]
        try:
            # Built inside the try: urllib's errors for a bad URL quote it.
            if lines:
                request = urllib.request.Request(
                    self._url + "/fail",
                    data=("\n".join(lines) + "\n").encode(),
                    method="POST",
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
            else:
                request = urllib.request.Request(self._url, method="GET")
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                response.read(1024)
        except urllib.error.HTTPError as exc:
            exc.close()  # it holds the response
            logger.warning("health: ping not delivered (HTTP %d)", exc.code)
            return False
        except Exception as exc:
            # The class only: some exceptions' text carries the host.
            logger.warning("health: ping not delivered (%s)", type(exc).__name__)
            return False
        if lines:
            logger.info("health: failure ping sent: %s", "; ".join(lines))
        else:
            logger.debug("health: ping sent")
        return True
