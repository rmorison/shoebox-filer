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

pdfplumber stays for text extraction, and it decodes the same untrusted
content, so it runs under the same bounds: :func:`run_bounded` is shared,
and the child runs one of two jobs, ``check`` (here) or ``read``
(:func:`receipt_index.pdf_reader.read_pdf`). Each has its own child, so a
PDF too costly to read is still judged on its own bytes.
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

logger = logging.getLogger(__name__)

PdfStatus = Literal["ok", "repaired", "unparseable"]

_MODULE = "receipt_index.pdf_validate"
# Enough for one item's attachment and rendition; a repair can be 25 MB.
_CACHE_SIZE = 4


@dataclass(frozen=True)
class PdfCheck:
    """A PDF's status, and its repaired bytes when it was ``repaired``."""

    status: PdfStatus
    repaired: bytes | None = None


@dataclass(frozen=True)
class PdfLimits:
    """Bounds on checking or reading one PDF, far below ``item_timeout_seconds``.

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
    import pikepdf  # in the child only: a read child doesn't pay for it

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
    import pikepdf

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


def _check_job(data: bytes) -> bytes:
    """The status on one line, then (for ``repaired``) the repair."""
    result = _judge(data)
    return result.status.encode() + b"\n" + (result.repaired or b"")


def _child_main() -> None:
    """Limit this process, run the job on the PDF on stdin, write its output.

    Arguments: the job (``check`` or ``read``), the memory and CPU limits,
    then the job's own arguments.
    """
    job = sys.argv[1]
    memory, cpu = (int(arg) for arg in sys.argv[2:4])
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    data = sys.stdin.buffer.read()
    if job == "check":
        output = _check_job(data)
    elif job == "read":
        from receipt_index.pdf_reader import read_job

        output = read_job(data, sys.argv[4:])
    else:
        sys.exit(f"unknown job {job!r}")
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# The caller's side
# ---------------------------------------------------------------------------

_cache: OrderedDict[bytes, PdfCheck] = OrderedDict()
_cache_lock = threading.Lock()


def run_bounded(job: str, data: bytes, limits: PdfLimits, *args: str) -> bytes | None:
    """The output of ``job`` run on ``data`` in a bounded child process.

    None, with the reason logged, when ``data`` is over the size cap or the
    child times out, is killed (a hit limit) or fails.
    """
    if len(data) > limits.max_bytes:
        logger.warning(
            "PDF of %d bytes is over the %d-byte limit; not running the %s",
            len(data),
            limits.max_bytes,
            job,
        )
        return None
    try:
        proc = subprocess.run(  # our own interpreter and module
            [
                sys.executable,
                "-I",
                "-m",
                _MODULE,
                job,
                str(limits.memory_bytes),
                str(limits.cpu_seconds),
                *args,
            ],
            input=data,
            capture_output=True,
            timeout=limits.wall_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "PDF %s timed out after %.0fs (%d bytes)",
            job,
            limits.wall_seconds,
            len(data),
        )
        return None
    if proc.returncode != 0:
        # A negative code is the signal: SIGXCPU for the CPU limit, SIGKILL...
        logger.warning(
            "PDF %s ended with exit code %d (%d bytes): %s",
            job,
            proc.returncode,
            len(data),
            proc.stderr.decode(errors="replace").strip()[-300:],
        )
        return None
    return proc.stdout


def _run_check(data: bytes, limits: PdfLimits) -> PdfCheck | None:
    """The child's judgement of ``data``, or None if it didn't finish cleanly."""
    output = run_bounded("check", data, limits)
    if output is None:
        return None
    status, _, repaired = output.partition(b"\n")
    if status not in (b"ok", b"repaired", b"unparseable"):
        logger.warning("PDF check gave no status (%d bytes)", len(data))
        return None
    if status == b"repaired":
        return PdfCheck(status="repaired", repaired=repaired)
    return PdfCheck(status="ok") if status == b"ok" else _UNPARSEABLE


def check_pdf(data: bytes, *, limits: PdfLimits = DEFAULT_LIMITS) -> PdfCheck:
    """Judge ``data`` as ``ok``, ``repaired`` (with the repair) or ``unparseable``.

    qpdf runs in a bounded child process; see the module docstring.
    """
    key = hashlib.sha256(data).digest()
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    result = _run_check(data, limits)
    if result is None:
        # Not cached: a timeout or a kill can be the host's load, not the file.
        logger.info("Treating the PDF as unparseable (%d bytes)", len(data))
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
