"""The integration suite fails closed when the devtest ports aren't set.

Without POSTGRES_HOST_PORT, GREENMAIL_SMTP_PORT and GREENMAIL_IMAP_PORT the
suite must skip without opening a connection to any port. Falling back to a
default port could reach another stack on a shared host, and the suite
rebuilds tables. This runs the integration suite in a subprocess under
``connect_guard``, which refuses and records every connection, and checks
that nothing was attempted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.integration.ports import missing_ports, parse_port

_ROOT = Path(__file__).resolve().parents[2]
_PORT_VARS = ("POSTGRES_HOST_PORT", "GREENMAIL_SMTP_PORT", "GREENMAIL_IMAP_PORT")
# Anything else that could tell a client where to connect.
_OTHER_VARS = ("DATABASE_URL", "MIGRATION_DATABASE_URL")


def _run_guarded(tmp_path: Path, ports: dict[str, str]) -> dict[str, Any]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _PORT_VARS + _OTHER_VARS and not k.startswith("PG")
    }
    env.update(ports)
    log = tmp_path / "guard.json"
    env["CONNECT_GUARD_LOG"] = str(log)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration",
            "-m",
            "integration",
            "-p",
            "tests.unit.connect_guard",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-q",
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert log.exists(), proc.stdout[-2000:] + proc.stderr[-2000:]
    record: dict[str, Any] = json.loads(log.read_text())
    record["output"] = proc.stdout[-2000:]
    record["returncode"] = proc.returncode
    return record


# The guard refuses every connection, so a port set here is never reached.
_SCENARIOS = {
    "all unset": {},
    "one not a port": {
        "POSTGRES_HOST_PORT": "45432",
        "GREENMAIL_SMTP_PORT": "43025",
        "GREENMAIL_IMAP_PORT": "not-a-port",
    },
}


@pytest.fixture(scope="module", params=list(_SCENARIOS))
def guarded_run(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    return _run_guarded(tmp_path_factory.mktemp("guard"), _SCENARIOS[request.param])


class TestWithoutThePortVariables:
    def test_no_connection_is_attempted(self, guarded_run: dict[str, Any]) -> None:
        assert guarded_run["attempts"] == [], guarded_run["output"]

    def test_no_fixture_or_test_body_runs(self, guarded_run: dict[str, Any]) -> None:
        assert guarded_run["fixture_setups"] == []
        assert guarded_run["test_calls"] == []

    def test_every_test_skips_and_says_which_variables(
        self, guarded_run: dict[str, Any]
    ) -> None:
        assert guarded_run["returncode"] == 0, guarded_run["output"]
        assert guarded_run["collected"] > 0
        reports = guarded_run["reports"]
        assert len(reports) == guarded_run["collected"]
        for report in reports:
            assert report["outcome"] == "skipped", report
            for var in _PORT_VARS:
                assert var in report["reason"], report


class TestMissingPorts:
    def test_all_set(self) -> None:
        env = dict.fromkeys(_PORT_VARS, "25432")
        assert missing_ports(env) == []

    @pytest.mark.parametrize("value", [None, "", " ", "abc", "0", "65536", "-1"])
    def test_unusable_values_count_as_missing(self, value: str | None) -> None:
        env = dict.fromkeys(_PORT_VARS, "25432")
        if value is None:
            del env["GREENMAIL_SMTP_PORT"]
        else:
            env["GREENMAIL_SMTP_PORT"] = value
        assert missing_ports(env) == ["GREENMAIL_SMTP_PORT"]

    def test_none_set(self) -> None:
        assert missing_ports({}) == list(_PORT_VARS)

    def test_parse_port(self) -> None:
        assert parse_port("23143") == 23143
        assert parse_port(" 23143 ") == 23143
