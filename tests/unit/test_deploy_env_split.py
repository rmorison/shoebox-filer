"""The production app containers hold no owner database credential.

The app containers are `scheduler` (the cron lines) and `ingest` (`serve`);
both read ../.env. Also checks the `ingest` service's shape and that the
crontab no longer runs ingest.

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

# The services that run the application and read ../.env.
APP_SERVICES = ("scheduler", "ingest")

# The app containers' database logins: the write login and the read login.
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


@pytest.mark.parametrize("name", APP_SERVICES)
def test_app_service_has_no_owner_variable(services: dict[str, Any], name: str) -> None:
    found = sorted(FORBIDDEN_IN_SCHEDULER & _environment(services, name).keys())
    assert found == [], f"{name} environment holds owner variables: {found}"


@pytest.mark.parametrize("name", APP_SERVICES)
def test_app_service_has_no_owner_secret_value(
    services: dict[str, Any], name: str
) -> None:
    db = _environment(services, "db")
    app = _environment(services, name)
    secrets = {secret: db[secret] for secret in OWNER_SECRETS if db.get(secret)}
    assert secrets.keys() == set(OWNER_SECRETS), "db lacks an owner secret to compare"
    leaks = sorted(
        f"{var} carries {secret}"
        for var, value in app.items()
        for secret, secret_value in secrets.items()
        if secret_value in value
    )
    assert leaks == [], f"{name} environment holds owner secrets: {leaks}"


@pytest.mark.parametrize("name", APP_SERVICES)
def test_app_service_has_no_owner_url(services: dict[str, Any], name: str) -> None:
    app = _environment(services, name)
    owner_urls = sorted(var for var, value in app.items() if OWNER_URL.search(value))
    assert owner_urls == [], f"{name} URLs log in as the owner: {owner_urls}"


@pytest.mark.parametrize("name", APP_SERVICES)
def test_app_service_keeps_its_logins(services: dict[str, Any], name: str) -> None:
    missing = sorted(REQUIRED_IN_SCHEDULER - _environment(services, name).keys())
    assert missing == [], f"{name} environment lacks: {missing}"


_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def _seconds(duration: str) -> int:
    """Seconds in a duration as `docker compose config` prints it (`21m0s`)."""
    match = _DURATION.fullmatch(duration)
    assert match and duration, f"unexpected duration {duration!r}"
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def test_ingest_service_runs_serve(services: dict[str, Any]) -> None:
    ingest = services["ingest"]
    assert ingest["entrypoint"] == ["receipt-index", "serve"]
    assert ingest["restart"] == "unless-stopped"
    assert ingest["init"] is True
    assert int(ingest["shm_size"]) >= 1024**3
    assert ingest["image"] == services["scheduler"]["image"]


def test_ingest_mounts_the_store_and_config(services: dict[str, Any]) -> None:
    """Without the store mount serve would write PDFs into the container layer."""
    targets = {v["target"]: v for v in services["ingest"]["volumes"]}
    assert targets["/app/data/receipts"]["type"] == "bind"
    assert not targets["/app/data/receipts"].get("read_only")
    assert targets["/app/receipt-index.yaml"]["type"] == "bind"
    assert services["ingest"]["environment"]["RECEIPT_INDEX_CONFIG"] == (
        "/app/receipt-index.yaml"
    )
    assert services["ingest"]["environment"]["PLAYWRIGHT_BROWSERS_PATH"] == (
        "/ms-playwright"
    )


def test_ingest_healthcheck_is_status_check(services: dict[str, Any]) -> None:
    check = services["ingest"]["healthcheck"]
    assert check["test"] == ["CMD", "receipt-index", "status", "--check"]
    assert _seconds(check["interval"]) == 60
    assert check["retries"] == 3
    assert _seconds(check["start_period"]) >= 60


def test_ingest_stop_grace_covers_the_item_timeout(services: dict[str, Any]) -> None:
    """SIGTERM lets the item in hand finish; Docker must not SIGKILL it first."""
    from receipt_index.config import IngestConfig

    floor = IngestConfig().item_timeout_seconds + 60
    grace = _seconds(services["ingest"]["stop_grace_period"])
    assert grace >= floor, f"stop_grace_period {grace}s is below {floor}s"


def test_crontab_runs_no_ingest() -> None:
    jobs = [
        line
        for line in (DEPLOY / "crontab").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert jobs, "crontab has no job lines"
    ingest = [line for line in jobs if re.search(r"receipt-index\s+ingest\b", line)]
    assert ingest == [], f"crontab still runs ingest: {ingest}"


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


def test_missing_env_db_fails_compose_config(tmp_path: Path) -> None:
    """A server without .env.db stops at `config`, before touching a container."""
    if not _compose_available():
        if os.environ.get("CI"):
            pytest.fail("docker compose is required in CI for the env split check")
        pytest.skip("docker compose is not installed")
    shutil.copytree(DEPLOY, tmp_path / "deploy")
    shutil.copy(DEPLOY / "example.env", tmp_path / ".env")
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "DOCKER_HOST"}}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(tmp_path / "deploy" / "docker-compose.yml"),
            "config",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert ".env.db" in result.stderr


SHOW_MIGRATION_URL = 'show:\n\t@echo "[$$MIGRATION_DATABASE_URL]"\n'


def _make_show(root: Path) -> str:
    if shutil.which("make") is None:
        if os.environ.get("CI"):
            pytest.fail("make is required in CI for the Makefile env check")
        pytest.skip("make is not installed")
    shutil.copy(REPO_ROOT / "Makefile", root / "Makefile")
    (root / "show.mk").write_text(SHOW_MIGRATION_URL)
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME"}}
    result = subprocess.run(
        ["make", "-s", "-f", "Makefile", "-f", "show.mk", "show"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=root,
    )
    return result.stdout.strip()


def test_makefile_exports_migration_url_from_env_db(tmp_path: Path) -> None:
    """`make migrate-up` on the server finds MIGRATION_DATABASE_URL in .env.db."""
    shutil.copy(DEPLOY / "example.env", tmp_path / ".env")
    shutil.copy(DEPLOY / "example.env.db", tmp_path / ".env.db")
    shown = _make_show(tmp_path)
    assert shown.startswith("[postgresql://receipt_index:"), shown
    assert shown.endswith("?sslmode=disable]"), shown


def test_makefile_without_env_db_keeps_dev_env(tmp_path: Path) -> None:
    """A dev checkout has one .env, which still sets MIGRATION_DATABASE_URL."""
    shutil.copy(REPO_ROOT / "example.env", tmp_path / ".env")
    assert _make_show(tmp_path) != "[]"
