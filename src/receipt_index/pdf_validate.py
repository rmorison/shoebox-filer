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

pdfplumber stays for text extraction; this module only judges and repairs.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Literal

import pikepdf

logger = logging.getLogger(__name__)

PdfStatus = Literal["ok", "repaired", "unparseable"]


@dataclass(frozen=True)
class PdfCheck:
    """A PDF's status, and its repaired bytes when it was ``repaired``."""

    status: PdfStatus
    repaired: bytes | None = None


def _strict_ok(data: bytes) -> bool:
    """Whether qpdf reads ``data`` without recovery, saves it, and warns of nothing."""
    try:
        with pikepdf.open(io.BytesIO(data), attempt_recovery=False) as pdf:
            pdf.save(io.BytesIO())
            return not pdf.get_warnings()
    except MemoryError:
        raise  # the host's state, not the file's: let the item be retried
    except Exception:  # any other qpdf failure means it can't be read
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
    except MemoryError:
        raise
    except Exception:  # any other qpdf failure means it can't be read
        logger.debug("PDF repair failed", exc_info=True)
        return None


def check_pdf(data: bytes) -> PdfCheck:
    """Judge ``data`` as ``ok``, ``repaired`` (with the repair) or ``unparseable``."""
    if _strict_ok(data):
        return PdfCheck(status="ok")
    repaired = _repair(data)
    if repaired is not None and _strict_ok(repaired):
        logger.info("Repaired a damaged PDF (%d bytes)", len(data))
        return PdfCheck(status="repaired", repaired=repaired)
    logger.info("PDF can't be parsed or repaired (%d bytes)", len(data))
    return PdfCheck(status="unparseable")
