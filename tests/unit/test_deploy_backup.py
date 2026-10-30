"""deploy/backup.sh names its dumps, and deploy/crontab schedules them.

Runs the real script under ``sh`` with stub ``psql``, ``pg_dump`` and ``date``
on ``PATH``, so no database or container is needed. The stubs log their
arguments, and ``date`` answers from STUB_DOW and STUB_MONTH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SH = REPO_ROOT / "deploy" / "backup.sh"
CRONTAB = REPO_ROOT / "deploy" / "crontab"

READ_URL = (
    "postgresql://receipt_index_read@db:5432/receipt_index"  # pragma: allowlist secret
)

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")

STUBS = {
    "psql": "echo receipt_index_read\n",
    # pg_dump --format=custom --no-password --file=<path> <url>
    "pg_dump": (
        'echo "$*" >> "$STUB_LOG"\n'
        'for a in "$@"; do case "$a" in --file=*) f="${a#--file=}";; esac; done\n'
        'printf "%s" "${STUB_CONTENT:-dump}" > "$f"\n'
    ),
    "date": (
        'case "$1" in\n'
        '  +%u) echo "$STUB_DOW";;\n'
        '  +%m) echo "$STUB_MONTH";;\n'
        '  *) echo "unexpected date $*" >&2; exit 9;;\n'
        "esac\n"
    ),
}


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
    return bin_dir


def _run(
    tmp_path: Path, stub_bin: Path, *args: str, content: str = "dump"
) -> subprocess.CompletedProcess[str]:
    backups = tmp_path / "backups"
    env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "BACKUP_DIR": str(backups),
        "RECEIPT_INDEX_READ_DATABASE_URL": READ_URL,
        "STUB_LOG": str(tmp_path / "pg_dump.log"),
        "STUB_CONTENT": content,
        "STUB_DOW": "3",
        "STUB_MONTH": "11",
    }
    return subprocess.run(
        ["sh", str(BACKUP_SH), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _files(tmp_path: Path) -> list[str]:
    backups = tmp_path / "backups"
    return sorted(p.name for p in backups.iterdir()) if backups.exists() else []


def test_nightly_writes_day_of_week_name(tmp_path: Path, stub_bin: Path) -> None:
    result = _run(tmp_path, stub_bin)
    assert result.returncode == 0, result.stderr
    assert _files(tmp_path) == ["receipt_index-3.dump"]
    assert READ_URL in (tmp_path / "pg_dump.log").read_text()


def test_nightly_argument_matches_no_argument(tmp_path: Path, stub_bin: Path) -> None:
    result = _run(tmp_path, stub_bin, "nightly")
    assert result.returncode == 0, result.stderr
    assert _files(tmp_path) == ["receipt_index-3.dump"]


def test_monthly_writes_month_name_as_read_login(
    tmp_path: Path, stub_bin: Path
) -> None:
    result = _run(tmp_path, stub_bin, "monthly")
    assert result.returncode == 0, result.stderr
    assert _files(tmp_path) == ["receipt_index-month-11.dump"]
    assert READ_URL in (tmp_path / "pg_dump.log").read_text()


def test_monthly_rerun_in_same_month_replaces_the_file(
    tmp_path: Path, stub_bin: Path
) -> None:
    assert _run(tmp_path, stub_bin, "monthly", content="first").returncode == 0
    result = _run(tmp_path, stub_bin, "monthly", content="second")
    assert result.returncode == 0, result.stderr
    assert _files(tmp_path) == ["receipt_index-month-11.dump"]
    assert (
        tmp_path / "backups" / "receipt_index-month-11.dump"
    ).read_text() == "second"


def test_unknown_argument_fails_without_dumping(tmp_path: Path, stub_bin: Path) -> None:
    result = _run(tmp_path, stub_bin, "weekly")
    assert result.returncode != 0
    assert _files(tmp_path) == []
    assert not (tmp_path / "pg_dump.log").exists()


def _cron_lines() -> list[list[str]]:
    lines = []
    for raw in CRONTAB.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line.split())
    return lines


def test_crontab_schedules_monthly_dump_on_the_first() -> None:
    monthly = [f for f in _cron_lines() if f[5:] == ["/app/backup.sh", "monthly"]]
    assert len(monthly) == 1, "expected exactly one monthly backup line"
    minute, hour, day, month, weekday = monthly[0][:5]
    assert minute.isdigit() and hour.isdigit()
    assert (day, month, weekday) == ("1", "*", "*")


def test_crontab_keeps_nightly_line_and_mirror() -> None:
    lines = [" ".join(f) for f in _cron_lines()]
    assert "17 3 * * * /app/backup.sh" in lines
    assert any(
        line.startswith("12,27,42,57 * * * * rclone copy /app/data/backups ")
        for line in lines
    )
