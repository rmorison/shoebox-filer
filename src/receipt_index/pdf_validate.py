"""PDF validation and repair at ingest, with pikepdf (qpdf).

Every PDF entering the evidence store is checked in strict mode
(evidence-store design, §9):

- ``ok``: qpdf opens it without recovery, saves it again, and warns of
  nothing. The warnings matter: qpdf opens a PDF whose ``%PDF-`` header is
  damaged in strict mode, with only a warning.
- ``repaired``: it fails that, but qpdf, with recovery, rewrites it into
  bytes that pass. The input is never changed; the rewrite is returned.
- ``unparseable``: anything else, including a PDF that needs a user password,
  and any error qpdf raises reading it: an attachment is evidence, stored
  whatever its state, so a hostile file must not fail the whole item.

The PDF is untrusted, and saving it decodes streams: a 2 KB file can inflate
to gigabytes or burn minutes of CPU. So qpdf never runs in the caller's
process. :func:`check_pdf` runs it in a child process (``python -m
receipt_index.pdf_validate``) under an address-space limit, a CPU limit and a
wall-clock timeout, and refuses input over a size cap. A child that hits a
limit, is killed or times out makes the PDF ``unparseable``; the caller's
memory and time are never at the file's mercy. A clean result is cached by
content, so an item's attachment and its pass-through rendition, the same
bytes, are checked once.

pdfplumber stays for text extraction; this module only judges and repairs.
"""

from __future__ import annotations

import hashlib
import io
import logging
import resource
import subprocess
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

import pikepdf

logger = logging.getLogger(__name__)

PdfStatus = Literal["ok", "repaired", "unparseable"]

_MODULE = "receipt_index.pdf_validate"
_CACHE_SIZE = 16


@dataclass(frozen=True)
class PdfCheck:
    """A PDF's status, and its repaired bytes when it was ``repaired``."""

    status: PdfStatus
    repaired: bytes | None = None


@dataclass(frozen=True)
class PdfLimits:
    """Bounds on checking one PDF. All are far below ``item_timeout_seconds``.

    A receipt is kilobytes to a few megabytes; Drive's own export limit is
    10 MB. The memory limit is the child's whole address space.
    """

    max_bytes: int = 25 * 1024 * 1024
    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 30
    wall_seconds: float = 60.0


DEFAULT_LIMITS = PdfLimits()

_UNPARSEABLE = PdfCheck(status="unparseable")


# ---------------------------------------------------------------------------
# The judgement itself: runs only in the child process
# ---------------------------------------------------------------------------


def _strict_ok(data: bytes) -> bool:
    """Whether qpdf reads ``data`` without recovery, saves it, and warns of nothing."""
    try:
        with pikepdf.open(io.BytesIO(data), attempt_recovery=False) as pdf:
            pdf.save(io.BytesIO())
            return not pdf.get_warnings()
    except Exception:  # any qpdf failure, a hit memory limit included
        logger.debug("Strict PDF check failed", exc_info=True)
        return False


def _repair(data: bytes) -> bytes | None:
    """qpdf's rewrite of ``data`` with recovery, or None if it can't read it.

    The file ID is derived from the content, so the same damaged bytes always
    give the same repair: a retry stores what the first attempt would have.
    """
    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            out = io.BytesIO()
            pdf.save(out, deterministic_id=True)
            return out.getvalue()
    except Exception:  # any qpdf failure, a hit memory limit included
        logger.debug("PDF repair failed", exc_info=True)
        return None


def _judge(data: bytes) -> PdfCheck:
    """The strict check, then the repair. Unbounded: call it only in the child."""
    if _strict_ok(data):
        return PdfCheck(status="ok")
    repaired = _repair(data)
    if repaired is not None and _strict_ok(repaired):
        return PdfCheck(status="repaired", repaired=repaired)
    return _UNPARSEABLE


def _child_main() -> None:
    """Limit this process, judge the PDF on stdin, write the result to stdout.

    Output is the status on one line, then (for ``repaired``) the repair.
    """
    memory, cpu = (int(arg) for arg in sys.argv[1:3])
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    result = _judge(sys.stdin.buffer.read())
    out = sys.stdout.buffer
    out.write(result.status.encode() + b"\n")
    if result.repaired is not None:
        out.write(result.repaired)
    out.flush()


# ---------------------------------------------------------------------------
# The caller's side
# ---------------------------------------------------------------------------

_cache: OrderedDict[bytes, PdfCheck] = OrderedDict()
_cache_lock = threading.Lock()


def _run_child(data: bytes, limits: PdfLimits) -> PdfCheck | None:
    """The child's judgement of ``data``, or None if it didn't finish cleanly."""
    try:
        proc = subprocess.run(  # our own interpreter and module
            [
                sys.executable,
                "-I",
                "-m",
                _MODULE,
                str(limits.memory_bytes),
                str(limits.cpu_seconds),
            ],
            input=data,
            capture_output=True,
            timeout=limits.wall_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "PDF check timed out after %.0fs (%d bytes); treating it as unparseable",
            limits.wall_seconds,
            len(data),
        )
        return None
    status, _, repaired = proc.stdout.partition(b"\n")
    if proc.returncode != 0 or status not in (b"ok", b"repaired", b"unparseable"):
        # A negative code is the signal: SIGXCPU for the CPU limit, SIGKILL...
        logger.warning(
            "PDF check ended with exit code %d (%d bytes); treating it as "
            "unparseable: %s",
            proc.returncode,
            len(data),
            proc.stderr.decode(errors="replace").strip()[-300:],
        )
        return None
    if status == b"repaired":
        return PdfCheck(status="repaired", repaired=repaired)
    return PdfCheck(status="ok") if status == b"ok" else _UNPARSEABLE


def check_pdf(data: bytes, *, limits: PdfLimits = DEFAULT_LIMITS) -> PdfCheck:
    """Judge ``data`` as ``ok``, ``repaired`` (with the repair) or ``unparseable``.

    qpdf runs in a bounded child process; see the module docstring.
    """
    if len(data) > limits.max_bytes:
        logger.warning(
            "PDF of %d bytes is over the %d-byte limit; treating it as unparseable",
            len(data),
            limits.max_bytes,
        )
        return _UNPARSEABLE
    key = hashlib.sha256(data).digest()
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    result = _run_child(data, limits)
    if result is None:
        # Not cached: a timeout or a kill can be the host's load, not the file.
        return _UNPARSEABLE
    if result.status == "repaired":
        logger.info("Repaired a damaged PDF (%d bytes)", len(data))
    elif result.status == "unparseable":
        logger.info("PDF can't be parsed or repaired (%d bytes)", len(data))
    with _cache_lock:
        _cache[key] = result
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return result


if __name__ == "__main__":
    _child_main()
