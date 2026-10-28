"""A pytest plugin that refuses every outbound connection and records it.

``test_integration_fail_closed`` loads it with ``-p`` into a pytest run of
the integration suite, so it is in place before any conftest or test module
is imported. Every guarded call records itself and raises before anything
reaches a port. libpq opens its sockets below Python's socket module, so
psycopg's connect functions are guarded directly too. Starting a subprocess,
which could connect on its own, is refused once collection ends; during
collection some imports run one (weasyprint runs ``ldconfig`` to find its
libraries), so those only run and are recorded.

The plugin also counts fixture setups and test calls, and writes the record
to the JSON file named by ``CONNECT_GUARD_LOG`` when the session ends.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from typing import TYPE_CHECKING, Any, NoReturn

import psycopg
import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

_record: dict[str, Any] = {
    "attempts": [],
    "fixture_setups": [],
    "test_calls": [],
    "reports": [],
    "collection_subprocesses": [],
}


class ConnectionRefusedByGuard(RuntimeError):  # noqa: N818 - reads as a sentence
    """Raised in place of any outbound connection."""


def _refuse(kind: str, *, method: bool = False) -> Any:
    def guarded(*args: Any, **kwargs: Any) -> NoReturn:
        # A guarded method's first argument is the object itself, whose repr
        # can fail; a function's first argument is the address or DSN.
        described = _describe(args[1:] if method else args, kwargs)
        _record["attempts"].append({"kind": kind, "args": described})
        raise ConnectionRefusedByGuard(f"connect guard: {kind} refused")

    return guarded


def _describe(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    try:
        return repr((args, kwargs))[:200]
    except Exception:
        return "<unprintable>"


_real_popen_init = subprocess.Popen.__init__
_collection_done = False


def _guarded_popen_init(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
    command = repr(args[:1] or kwargs.get("args"))[:200]
    if _collection_done:
        _record["attempts"].append({"kind": "subprocess.Popen", "args": command})
        raise ConnectionRefusedByGuard("connect guard: subprocess.Popen refused")
    _record["collection_subprocesses"].append(command)
    _real_popen_init(self, *args, **kwargs)


socket.socket.connect = _refuse("socket.connect", method=True)  # type: ignore[method-assign]
socket.socket.connect_ex = _refuse("socket.connect_ex", method=True)  # type: ignore[method-assign]
socket.create_connection = _refuse("socket.create_connection")
psycopg.connect = _refuse("psycopg.connect")
psycopg.Connection.connect = _refuse("psycopg.Connection.connect")  # type: ignore[method-assign]
psycopg.AsyncConnection.connect = _refuse("psycopg.AsyncConnection.connect")  # type: ignore[method-assign]
subprocess.Popen.__init__ = _guarded_popen_init  # type: ignore[method-assign]


def pytest_collection_finish(session: pytest.Session) -> None:
    global _collection_done
    _collection_done = True


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef[Any]) -> Generator[None]:
    _record["fixture_setups"].append(fixturedef.argname)
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None]:
    _record["test_calls"].append(item.nodeid)
    return (yield)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "setup" or report.outcome != "passed":
        reason = ""
        if report.skipped and isinstance(report.longrepr, tuple):
            reason = str(report.longrepr[2])
        _record["reports"].append(
            {
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "reason": reason,
            }
        )


def pytest_sessionfinish(session: pytest.Session) -> None:
    _record["collected"] = session.testscollected
    with open(os.environ["CONNECT_GUARD_LOG"], "w") as fh:
        json.dump(_record, fh)
