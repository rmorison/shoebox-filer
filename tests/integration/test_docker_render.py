"""In-container checks for the production image.

These tests exercise the image built from ``Dockerfile`` rather than the host
environment. They are the load-bearing proof for the deployment: a rendering
gap in the container silently DLQs receipts (``ingest_log``), and DLQ'd items
are never retried, so both render engines and the scheduler/mirror/backup
binaries must be verified inside the image before it goes unattended.

The image is not built here (too slow). Point ``RECEIPT_INDEX_IMAGE`` at a
pre-built tag, or build the default ``receipt-index:latest`` first::

    docker build -t receipt-index:latest .
    make test-integration
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

IMAGE = os.environ.get("RECEIPT_INDEX_IMAGE", "receipt-index:latest")

# Cold-start Chromium in a container is slow; a render smoke test is still
# far below this ceiling.
_TIMEOUT_SECONDS = 180


def _image_available() -> bool:
    """Return True if the docker CLI works and the target image exists."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", IMAGE],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _image_available(),
        reason=(
            f"docker unavailable or image {IMAGE!r} not built "
            "(build it, or set RECEIPT_INDEX_IMAGE)"
        ),
    ),
]


@pytest.fixture(autouse=True)
def _truncate_receipts() -> None:
    """Override the package-level DB fixture; these tests need no database."""


def _docker_run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the image with an overridden entrypoint and capture its output.

    ``--shm-size=1g`` mirrors the production compose setting: Chromium
    crashes on the 64 MB default ``/dev/shm``.
    """
    return subprocess.run(
        ["docker", "run", "--rm", "--shm-size=1g", *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def _run_python(script: str) -> str:
    """Run a Python snippet inside the image, returning its stdout.

    Fails the test with the container's stderr if the snippet exits non-zero.
    """
    result = _docker_run("--entrypoint", "python", IMAGE, "-c", script)
    assert result.returncode == 0, (
        f"python snippet failed in {IMAGE} (exit {result.returncode})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout.strip()


# The renderer's private engine functions are called directly on purpose: the
# public path (`_html_to_pdf_bytes`) falls back from Playwright to weasyprint,
# which would mask exactly the failure these tests exist to catch.

_PLAYWRIGHT_SCRIPT = """
import os

from receipt_index.renderer import _html_to_pdf_playwright

assert os.getuid() == 1000, f"expected uid 1000, got {os.getuid()}"

html = (
    "<html><body><h1>ACME Hardware</h1>"
    "<table>"
    "<tr><td>Widget</td><td style='text-align:right'>$42.99</td></tr>"
    "<tr><td><strong>Total</strong></td><td><strong>$42.99</strong></td></tr>"
    "</table></body></html>"
)
pdf = _html_to_pdf_playwright(html)
assert pdf[:5] == b"%PDF-", repr(pdf[:20])
print(len(pdf))
"""

_WEASYPRINT_SCRIPT = """
import os
from datetime import UTC, datetime

from receipt_index.models import RawReceipt
from receipt_index.renderer import render_pdf

assert os.getuid() == 1000, f"expected uid 1000, got {os.getuid()}"

raw = RawReceipt(
    source_id="docker-smoke-1",
    source_name="smoke",
    source_type="imap",
    date=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
    subject="Your receipt",
    sender="billing@example.com",
    text_body="Total: $42.99\\nThank you for your business.",
)
pdf = render_pdf(raw)
assert pdf[:5] == b"%PDF-", repr(pdf[:20])
print(len(pdf))
"""

_BROWSERS_PATH_SCRIPT = """
import os

from receipt_index.renderer import _html_to_pdf_playwright

path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
assert path == "/ms-playwright", f"unexpected browsers path: {path!r}"

entries = sorted(os.listdir(path))
assert any(e.startswith("chromium") for e in entries), entries

pdf = _html_to_pdf_playwright("<html><body><p>baked</p></body></html>")
assert pdf[:5] == b"%PDF-", repr(pdf[:20])
print(" ".join(entries))
"""


class TestInContainerRendering:
    """Both render engines must work inside the image as uid 1000."""

    def test_playwright_renders_html_receipt(self) -> None:
        """Chromium renders an HTML receipt body to a non-trivial PDF."""
        size = int(_run_python(_PLAYWRIGHT_SCRIPT))
        assert size > 1000

    def test_weasyprint_renders_text_receipt(self) -> None:
        """The text path renders via weasyprint, proving the pango libs."""
        size = int(_run_python(_WEASYPRINT_SCRIPT))
        assert size > 500

    def test_browsers_path_is_baked_into_the_image(self) -> None:
        """Rendering works without the host supplying the browsers path.

        No ``-e`` flag is passed to ``docker run``, so the container must find
        Chromium under the image's own baked ``PLAYWRIGHT_BROWSERS_PATH``
        (``/ms-playwright``). Host-env leakage via ``docker run`` is impossible
        by construction; the compose-level ``env_file`` override is the real
        leak vector and is prevented by the explicit ``environment:`` entry in
        ``deploy/docker-compose.yml``.
        """
        entries = _run_python(_BROWSERS_PATH_SCRIPT).split()

        assert any(entry.startswith("chromium") for entry in entries), entries


class TestBundledBinaries:
    """Scheduler, mirror, and backup binaries ship in the image."""

    @pytest.mark.parametrize(
        ("entrypoint", "args"),
        [
            ("supercronic", ["-version"]),
            ("rclone", ["version"]),
            ("pg_dump", ["--version"]),
        ],
    )
    def test_binary_runs(self, entrypoint: str, args: list[str]) -> None:
        """Each pinned binary is on PATH and executable as uid 1000."""
        result = _docker_run("--entrypoint", entrypoint, IMAGE, *args)

        assert result.returncode == 0, (
            f"{entrypoint} failed (exit {result.returncode})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert result.stdout.strip()

    def test_pg_dump_is_version_18(self) -> None:
        """pg_dump must be v18: an older client cannot dump the v18 server."""
        result = _docker_run("--entrypoint", "pg_dump", IMAGE, "--version")

        assert result.returncode == 0, result.stderr
        assert "(PostgreSQL) 18." in result.stdout, result.stdout
