"""PDF text extraction with pdfplumber, in a bounded child process.

A PDF with too little text, such as a scan, gives an empty string. The
caller then sends the PDF itself to the extraction agent as a document
block (see ADR-0004), so this module makes no model calls.

The PDF is untrusted, and reading its text decodes its page content: a file
of under a kilobyte can hold pdfplumber for minutes or inflate to gigabytes.
So pdfplumber never runs in the caller's process. :func:`read_pdf` runs it
through PDF validation's runner (:func:`receipt_index.pdf_validate.run_bounded`),
under its own address-space, CPU, wall-clock and size limits, and reads
no text from a PDF over a page cap (:data:`RECEIPT_BOUNDS`). A PDF that hits
a limit or the cap reads as unreadable, the same as one pdfplumber can't open.

A bank or card statement is read by :func:`read_statement` instead, under
:data:`~receipt_index.pdf_validate.STATEMENT_BOUNDS`. It keeps each page's
text apart, so a prompt can mark pages, and when it can't give text it says
why: the statement pipeline rejects some of those reasons and retries
others. It also applies the statement's text checks, which aren't PDF
bounds: a scan (too little text) and a token cap on the page-marked text.
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Literal

from receipt_index.pdf_validate import (
    RECEIPT_BOUNDS,
    STATEMENT_BOUNDS,
    PdfBounds,
    PdfLimits,
    StopReason,
    run_bounded,
    run_bounded_job,
)

logger = logging.getLogger(__name__)

# Minimum non-whitespace characters to consider pdfplumber output valid.
# A minimal receipt must contain at least a vendor name and amount; 20 chars
# provides a buffer while catching truly empty extractions from image-only PDFs.
_MIN_TEXT_LENGTH = 20


# A statement's text is capped by an estimate of its tokens, not its
# characters: numeric, columnar text tokenises more densely than prose, at
# about 2.5 to 3 characters a token. Counting 2.5 keeps the estimate high.
# With the prompt (about 1,500 tokens) on top, each model call's input stays
# under the 100,000-token point where the model's price rises.
STATEMENT_TOKEN_CAP = 85_000
_CHARS_PER_TOKEN_TENTHS = 25  # 2.5 characters a token, in integer tenths

StatementReadReason = Literal[
    "pages",
    "size",
    "memory",
    "crash",
    "unreadable",
    "timeout",
    "failed",
    "text",
    "scan",
]

# What pdfplumber gives for a glyph whose font has no usable text map.
_CID = re.compile(r"\(cid:\d+\)")

# Characters dropped from a statement's text before anything else: NUL and
# the other C0 and C1 controls (but not a newline or a tab), bidirectional
# overrides, isolates and marks, and zero-width characters. None carries
# statement content; each can hide or reorder text for a reader or a model.
_DROPPED = dict.fromkeys(
    [
        *(c for c in range(0x20) if c not in (0x09, 0x0A)),
        *range(0x7F, 0xA0),
        0x061C,
        0x200E,
        0x200F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
        0x200B,
        0x200C,
        0x200D,
        0x2060,
        0xFEFF,
    ]
)

# How a bounded child's stop reads for a statement. A child that wrote past
# the output cap had more text than any statement under the token cap.
_STATEMENT_STOP: dict[StopReason, StatementReadReason] = {
    "size": "size",
    "timeout": "timeout",
    "memory": "memory",
    "crash": "crash",
    "output": "text",
    "failed": "failed",
}


@dataclass(frozen=True)
class StatementRead:
    """A statement's text, page by page, or why there is none.

    ``page_texts`` is set exactly when ``reason`` is None. ``pages`` is the
    page count when it is known. ``reason``:

    - ``pages``, ``size``, ``memory``: over a bound of ``STATEMENT_BOUNDS``.
      A PDF with a huge page tree hits ``memory`` or ``timeout`` before its
      page count is known, not ``pages``;
    - ``crash``: the reader died of a signal, such as a parser segfault;
    - ``timeout``: the read hit its CPU or wall-clock limit or was killed;
      it can be the host's load, so the caller retries it;
    - ``failed``: the reader exited with an error of its own (a full disk,
      no file descriptors, a broken install), not a verdict on the file, so
      the caller retries it too;
    - ``unreadable``: not a PDF, needs a password, or pdfplumber can't open
      it or failed reading it;
    - ``scan``: fewer than 20 readable characters on every page together,
      or no more readable characters than glyphs. A readable character is
      a non-whitespace character outside a ``(cid:N)`` glyph (what a font
      with no text map gives); each glyph counts as one. The test is over
      the whole document, not per page, so a readable header over blank
      or glyph-only pages passes here and is left to the extraction gates;
    - ``text``: the page-marked text is over :data:`STATEMENT_TOKEN_CAP`
      estimated tokens. The cap is applied here, once; callers don't
      apply it again.

    ``encrypted``: the PDF has an encryption dictionary and opened without a
    password (an owner password only). Such a PDF is read like any other,
    whatever its permission flags say.
    """

    page_texts: tuple[str, ...] | None
    pages: int | None = None
    reason: StatementReadReason | None = None
    encrypted: bool = False


@dataclass(frozen=True)
class PdfRead:
    """What pdfplumber found in one PDF. ``text`` is None when not read."""

    pages: int
    text: str | None
    encrypted: bool


def extract_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes with pdfplumber, in a bounded child.

    Args:
        pdf_bytes: Raw PDF file content.

    Returns:
        The extracted text, or an empty string when there is too little
        text to use (fewer than 20 non-whitespace characters), as in a
        scanned or image-only PDF, or when the PDF can't be read, a hit
        limit included.
    """
    read = read_pdf(pdf_bytes)
    text = (read.text or "") if read is not None else ""
    if _is_sufficient_text(text):
        logger.debug("pdfplumber extracted %d chars from PDF", len(text))
        return text

    logger.info(
        "pdfplumber extracted insufficient text (%d non-whitespace chars)",
        len("".join(text.split())),
    )
    return ""


def read_pdf(
    data: bytes,
    *,
    text_up_to_pages: int | None = None,
    limits: PdfLimits = RECEIPT_BOUNDS.read,
    max_pages: int = RECEIPT_BOUNDS.max_read_pages,
) -> PdfRead | None:
    """A PDF's page count, text and encryption, or None when it can't be read.

    pdfplumber runs in a bounded child (see the module docstring); a limit,
    kill or timeout means unreadable, and so does a PDF over ``max_pages``,
    whose text is never read. With ``text_up_to_pages``, a longer PDF's text
    isn't read either, but its page count is returned.
    """
    limit = max_pages if text_up_to_pages is None else min(text_up_to_pages, max_pages)
    output = run_bounded("read", data, limits, str(limit))
    if output is None:
        return None
    try:
        found = json.loads(output)
        read = None if found is None else PdfRead(**found)
    except (ValueError, TypeError):
        logger.warning("PDF read gave no result (%d bytes)", len(data))
        return None
    if read is None:
        logger.info("pdfplumber can't read the PDF (%d bytes)", len(data))
    if read is not None and read.pages > max_pages:
        logger.info(
            "PDF of %d pages is over the %d-page cap; not reading its text",
            read.pages,
            max_pages,
        )
        return None
    return read


def read_statement(
    data: bytes, *, bounds: PdfBounds = STATEMENT_BOUNDS
) -> StatementRead:
    """A statement's per-page text under ``bounds``, or why there is none.

    pdfplumber runs in a bounded child, as for :func:`read_pdf`, but the
    limits, page cap and stop reasons are the statement's. The child's
    output is capped, so the parent never parses more than the token cap
    could let through. Each page's text is normalised once, here (see
    :func:`normalise_statement_text`), before any check. No text from the
    statement is logged.
    """
    max_pages = bounds.max_read_pages
    run = run_bounded_job("read", data, bounds.read, str(max_pages), "pages")
    if run.output is None:
        reason = _STATEMENT_STOP[run.stop or "failed"]
        logger.info("Statement PDF not read: %s (%d bytes)", reason, len(data))
        return StatementRead(None, reason=reason)
    try:
        found = json.loads(run.output)
        if found is not None:
            pages = int(found["pages"])
            texts = found["page_texts"]
            encrypted = found["encrypted"]
            if not isinstance(encrypted, bool):
                raise TypeError("encrypted")
            if texts is not None and not (
                isinstance(texts, list) and all(isinstance(x, str) for x in texts)
            ):
                raise TypeError("page_texts")
    except (ValueError, TypeError, KeyError):
        logger.warning("Statement PDF read gave no result (%d bytes)", len(data))
        return StatementRead(None, reason="unreadable")
    if found is None or pages < 1:
        logger.info("pdfplumber can't read the statement PDF (%d bytes)", len(data))
        return StatementRead(None, reason="unreadable")
    if pages > max_pages or texts is None:
        logger.info(
            "Statement PDF of %d pages is over the %d-page cap", pages, max_pages
        )
        return StatementRead(None, pages=pages, reason="pages", encrypted=encrypted)
    texts = [normalise_statement_text(text) for text in texts]
    # The token cap is a length check, so it runs before anything that
    # looks at each character.
    tokens = estimate_tokens(page_marked_text(texts))
    if tokens > STATEMENT_TOKEN_CAP:
        logger.info(
            "Statement text of about %d tokens is over the %d-token cap",
            tokens,
            STATEMENT_TOKEN_CAP,
        )
        return StatementRead(None, pages=pages, reason="text", encrypted=encrypted)
    if not _is_usable_statement_text("".join(texts)):
        logger.info("Statement PDF has too little text: a scan (%d pages)", pages)
        return StatementRead(None, pages=pages, reason="scan", encrypted=encrypted)
    return StatementRead(tuple(texts), pages=pages, encrypted=encrypted)


def normalise_statement_text(text: str) -> str:
    """``text`` without control, bidirectional and zero-width characters.

    Drops NUL and the other C0 and C1 controls except a newline and a tab,
    bidirectional overrides, isolates and marks, and zero-width characters.
    Nothing else changes.
    """
    return text.translate(_DROPPED)


def _non_space(text: str) -> int:
    """How many non-whitespace characters ``text`` has, copying nothing."""
    return sum(1 for char in text if not char.isspace())


def _is_usable_statement_text(text: str) -> bool:
    """Whether a statement's text is enough to extract from.

    At least 20 readable non-whitespace characters, and more readable
    characters than unmapped glyphs, each ``(cid:N)`` counting as one glyph.
    A font with no text map gives runs like ``(cid:12)(cid:7)`` that would
    reach the model as noise; a disclosures page in such a font beside
    readable transaction pages is still read.
    """
    glyphs = 0
    markup = 0
    for match in _CID.finditer(text):
        glyphs += 1
        markup += len(match.group())  # a glyph's markup has no whitespace
    usable = _non_space(text) - markup
    return usable >= _MIN_TEXT_LENGTH and usable > glyphs


def page_marked_text(page_texts: tuple[str, ...] | list[str]) -> str:
    """A statement's text with each page marked, as a prompt gives it."""
    return "\n\n".join(
        f"[Page {number}]\n{text}" for number, text in enumerate(page_texts, 1)
    )


def estimate_tokens(text: str) -> int:
    """A high estimate of ``text``'s tokens: 2.5 characters a token, rounded up."""
    return -(-len(text) * 10 // _CHARS_PER_TOKEN_TENTHS)


def read_job(data: bytes, args: list[str]) -> bytes:
    """The child's ``read`` job, as JSON.

    Arguments: the page cap, then ``pages`` for a statement's per-page read
    (:func:`_read_pages`); otherwise :func:`_read_unbounded`.
    """
    limit = int(args[0])
    if args[1:] == ["pages"]:
        found = _read_pages(data, text_up_to_pages=limit)
        return json.dumps(
            None
            if found is None
            else {"pages": found[0], "page_texts": found[1], "encrypted": found[2]}
        ).encode()
    read = _read_unbounded(data, text_up_to_pages=limit)
    return json.dumps(None if read is None else asdict(read)).encode()


def _read_unbounded(
    data: bytes, *, text_up_to_pages: int | None = None
) -> PdfRead | None:
    """pdfplumber's read of ``data``. Unbounded: call it only in the child.

    Any failure, on opening or on a page, means unreadable, never a scan.
    """
    found = _read_pages(data, text_up_to_pages=text_up_to_pages)
    if found is None:
        return None
    pages, texts, encrypted = found
    if texts is None:
        return PdfRead(pages, None, encrypted)
    return PdfRead(pages, "\n\n".join(texts).strip(), encrypted)


def _read_pages(
    data: bytes, *, text_up_to_pages: int | None = None
) -> tuple[int, list[str] | None, bool] | None:
    """Page count, each page's text and encryption. Unbounded: child only.

    The text is None when the PDF has more pages than ``text_up_to_pages``.
    Any failure, on opening or on a page, gives None.
    """
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = len(pdf.pages)
            encrypted = pdf.doc.encryption is not None
            if text_up_to_pages is not None and pages > text_up_to_pages:
                return pages, None, encrypted
            texts = []
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
                # pdfplumber keeps each page's layout objects until the file
                # closes; a long document would otherwise outgrow the limit.
                page.close()
    except Exception as exc:  # any failure means unreadable
        if _out_of_memory(exc):
            raise MemoryError from exc  # the child reports the memory limit
        logger.debug("pdfplumber can't read the PDF", exc_info=True)
        return None
    return pages, texts, encrypted


def _out_of_memory(exc: BaseException) -> bool:
    """Whether ``exc`` is, or was caused by, running out of memory.

    pdfplumber wraps what pdfminer raises, a MemoryError included.
    """
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, MemoryError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _is_sufficient_text(text: str) -> bool:
    """Check if extracted text meets the minimum quality threshold."""
    stripped = "".join(text.split())
    return len(stripped) >= _MIN_TEXT_LENGTH
