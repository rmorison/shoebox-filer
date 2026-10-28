"""The production scheduler container holds no owner database credential.

Lays out a scratch clone root from the templates in ``deploy/`` and asks
``docker compose config`` what environment each service would get, so the
check follows compose's own env_file resolution rather than a re-parse of it.
Needs only the docker CLI with the compose plugin, not a daemon.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"

# Names that carry, or exist only to set, an owner or superuser credential,
# plus the bare login passwords, which only db-init reads.
FORBIDDEN_IN_SCHEDULER = {
    "MIGRATION_DATABASE_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "RECEIPT_INDEX_PASSWORD",
    "RECEIPT_INDEX_WRITE_PASSWORD",
    "RECEIPT_INDEX_READ_PASSWORD",
}

# The credentials whose values must not reach the scheduler under any name.
OWNER_SECRETS = ("POSTGRES_PASSWORD", "RECEIPT_INDEX_PASSWORD")

# What the postgres image and deploy/db-init/01-create-roles.sh read.
REQUIRED_IN_DB = {
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "RECEIPT_INDEX_PASSWORD",
    "RECEIPT_INDEX_WRITE_PASSWORD",
    "RECEIPT_INDEX_READ_PASSWORD",
}

# The scheduler's database logins: the write login and the read login.
REQUIRED_IN_SCHEDULER = {
    "DATABASE_URL",
    "RECEIPT_INDEX_READ_DATABASE_URL",
    "RECEIPT_INDEX_MCP_DATABASE_URL",
}

OWNER_URL = re.compile(r"://(receipt_index|postgres)(:|@)")


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def services(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    if not _compose_available():
        if os.environ.get("CI"):
            pytest.fail("docker compose is required in CI for the env split check")
        pytest.skip("docker compose is not installed")

    root = tmp_path_factory.mktemp("clone")
    shutil.copytree(DEPLOY, root / "deploy")
    shutil.copy(DEPLOY / "example.env", root / ".env")
    if (DEPLOY / "example.env.db").exists():
        shutil.copy(DEPLOY / "example.env.db", root / ".env.db")

    # A clean environment: nothing exported in the caller's shell may leak
    # into interpolation or stand in for a missing file.
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "DOCKER_HOST"}}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(root / "deploy" / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=root,
    )
    assert result.returncode == 0, result.stderr
    services: dict[str, Any] = json.loads(result.stdout)["services"]
    return services


def _environment(services: dict[str, Any], name: str) -> dict[str, str]:
    return {k: v or "" for k, v in (services[name].get("environment") or {}).items()}


def test_scheduler_has_no_owner_variable(services: dict[str, Any]) -> None:
    found = sorted(FORBIDDEN_IN_SCHEDULER & _environment(services, "scheduler").keys())
    assert found == [], f"scheduler environment holds owner variables: {found}"


def test_scheduler_has_no_owner_secret_value(services: dict[str, Any]) -> None:
    db = _environment(services, "db")
    scheduler = _environment(services, "scheduler")
    secrets = {name: db[name] for name in OWNER_SECRETS if db.get(name)}
    assert secrets.keys() == set(OWNER_SECRETS), "db lacks an owner secret to compare"
    leaks = sorted(
        f"{var} carries {secret}"
        for var, value in scheduler.items()
        for secret, secret_value in secrets.items()
        if secret_value in value
    )
    assert leaks == [], f"scheduler environment holds owner secrets: {leaks}"


def test_scheduler_has_no_owner_url(services: dict[str, Any]) -> None:
    scheduler = _environment(services, "scheduler")
    owner_urls = sorted(
        var for var, value in scheduler.items() if OWNER_URL.search(value)
    )
    assert owner_urls == [], f"scheduler URLs log in as the owner: {owner_urls}"


def test_scheduler_keeps_its_logins(services: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_IN_SCHEDULER - _environment(services, "scheduler").keys())
    assert missing == [], f"scheduler environment lacks: {missing}"


def test_db_has_what_init_needs(services: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_IN_DB - _environment(services, "db").keys())
    assert missing == [], f"db environment lacks: {missing}"


def _names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text().splitlines():
        match = re.match(r"([A-Z][A-Z0-9_]*)=", line)
        if match:
            names.add(match.group(1))
    return names


def test_example_files_do_not_overlap() -> None:
    both = sorted(_names(DEPLOY / "example.env") & _names(DEPLOY / "example.env.db"))
    assert both == [], f"set in both example.env and example.env.db: {both}"
