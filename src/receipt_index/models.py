"""Domain and extraction models for receipt indexing."""

from __future__ import annotations

import re
import traceback
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    ModelWrapValidatorHandler,
    computed_field,
    field_validator,
    model_validator,
)


@dataclass
class Attachment:
    """An email attachment.

    ``filename`` is what the extractor is shown: the MIME file name, or a
    stand-in (``unnamed``, or an inline image's Content-ID) for a part that
    has none. ``mime_filename`` is the MIME file name alone, None for a
    stand-in; it is the raw name the evidence store records and matches.
    """

    filename: str
    content_type: str
    data: bytes
    mime_filename: str | None = None


@dataclass
class RawReceipt:
    """Raw receipt data from a source adapter.

    Supports both email-based (IMAP) and file-based (Google Drive) sources.
    Email fields (subject, sender, html_body, text_body, attachments) are used
    by the IMAP adapter. File fields (file_name, file_content, file_content_type)
    are used by the Drive adapter.

    ``raw_bytes`` is the whole original exactly as fetched: an email's RFC 822
    bytes, or a binary Drive file's bytes. It is None for a native Google Doc
    (``file_content`` is then its PDF export, not the original) and for items
    built elsewhere than an adapter.
    """

    source_id: str
    source_name: str
    source_type: str
    date: datetime
    subject: str = ""
    sender: str = ""
    html_body: str | None = None
    text_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    file_name: str | None = None
    file_content: bytes | None = None
    file_content_type: str | None = None
    raw_bytes: bytes | None = None


@dataclass
class ItemRef:
    """One item a source listed: enough to identify it without its body.

    IMAP refs come from a header-only fetch; Drive refs from file metadata.
    ``skip_reason`` marks an item the pipeline records as skipped without
    fetching it (a Drive file of an unsupported type).
    """

    source_id: str
    source_type: str
    subject: str = ""
    sender: str = ""
    date: datetime | None = None
    imap_uid: int | None = None
    drive_meta: dict[str, Any] | None = None
    file_name: str | None = None
    skip_reason: str | None = None
    # Set when the source refused even this item's headers: fetching it
    # fails as an item error, so it gets a failed row and the cursor passes.
    unreadable: str | None = None


@dataclass
class SourceRead:
    """What one read of a source returned: its items and cursor candidates.

    ``reset`` names why a stored cursor was discarded ("uidvalidity changed",
    "invalid page token", "account changed"); a source with no stored cursor
    yet is a first run, not a reset.
    """

    items: list[ItemRef]
    reset: str | None = None
    # IMAP: the folder's UIDVALIDITY, the UID the search started above, and
    # every UID the search returned (a UID with no ref holds the cursor).
    uidvalidity: int | None = None
    start_uid: int = 0
    searched_uids: list[int] = field(default_factory=list)
    # Drive: the token to save once every item is handled, and its account.
    drive_page_token: str | None = None
    drive_account_id: str | None = None
    # Drive: something was left for a later read (settle time), so the
    # token must not move.
    held_back: bool = False


class SyncState(BaseModel):
    """A source's stored read position (``receipt.sync_state``)."""

    id: UUID
    source_name: str
    source_type: str
    folder: str
    imap_uidvalidity: int | None = None
    imap_last_uid: int | None = None
    drive_page_token: str | None = None
    drive_account_id: str | None = None
    archive_enabled_at: datetime | None = None
    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    resets: int = 0
    created_at: datetime
    updated_at: datetime


class FieldConfidence(BaseModel):
    """The model's own confidence in each extracted field, from 0 to 1.

    An estimate, not a measured probability: good for ordering a review,
    not for a hard threshold. ``order_number`` and ``payment`` are null when
    their field is; ``kind`` is null on output from before it was asked for.
    """

    vendor: float = Field(ge=0.0, le=1.0)
    amount: float = Field(ge=0.0, le=1.0)
    paid_amount: float = Field(ge=0.0, le=1.0)
    date: float = Field(ge=0.0, le=1.0)
    order_number: float | None = Field(default=None, ge=0.0, le=1.0)
    payment: float | None = Field(default=None, ge=0.0, le=1.0)
    kind: float | None = Field(default=None, ge=0.0, le=1.0)


def _blank_to_none(value: object) -> object:
    """A string stripped, and None when nothing is left."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


# Characters that print nothing: format characters (zero-width space and
# joiner, soft hyphen), combining marks, control characters other than
# whitespace, unassigned, private-use and surrogate code points, and the
# blank filler letters.
_INVISIBLE = frozenset({"Cf", "Mn", "Me", "Cc", "Cn", "Co", "Cs"})
_BLANK_LETTERS = frozenset("\u115f\u1160\u3164\uffa0\u2800")


def _is_invisible(ch: str) -> bool:
    if ch in _BLANK_LETTERS:
        return True
    return not ch.isspace() and unicodedata.category(ch) in _INVISIBLE


def _fold(ch: str) -> str:
    """One character with its digit as ASCII, and full-width punctuation as
    its ASCII form; a superscript, subscript or circled digit (a footnote
    mark) is left alone, being no digit of a card."""
    if ch.isascii():
        return ch
    if ch.isdecimal():
        return str(unicodedata.decimal(ch))
    folded = unicodedata.normalize("NFKC", ch)
    return folded if len(folded) == 1 and not folded.isdigit() else ch


@dataclass(frozen=True)
class _Visible:
    """Text as a card is read from it.

    ``text`` is the original folded (``_fold``) with its invisible characters
    removed; ``index`` gives each kept character's position in the original;
    ``soft`` holds each position in ``text`` where invisible characters were
    removed just before it. A soft break may be nothing (a zero-width space
    inside a group) or a group boundary (the only break between numbers).
    """

    text: str
    index: list[int]
    soft: frozenset[int]


def _visible(text: str) -> _Visible:
    chars: list[str] = []
    index: list[int] = []
    soft: set[int] = set()
    pending = False
    for i, ch in enumerate(text):
        if _is_invisible(ch):
            pending = True
            continue
        if pending:
            soft.add(len(chars))
            pending = False
        chars.append(_fold(ch))
        index.append(i)
    return _Visible("".join(chars), index, frozenset(soft))


_GROUPED_DIGITS = re.compile(r"[0-9\s\-\u2010-\u2015\u2212]+")
_DIGITS = re.compile(r"[0-9]+")


def last_four_digits(value: object) -> str | None:
    """The last four digits of the card or account in ``value``, or None.

    Only the last four digits are ever kept, so a longer number is cut here,
    whatever its type. A value that is only digit groups (``4111 1111 1111
    1111``) is read as one number; a value holding one card number among
    other text gives that card's, when no other number of four or more
    digits disagrees. Otherwise the one group of four or more digits is the
    card's (``Visa ending 4242, exp 12/25`` gives ``4242``); with none, or
    more than one, it is None rather than a guess. The result always matches
    ``^[0-9]{4}$``: the raw value never reaches a pattern check whose
    validation error would echo it into a logged traceback.
    """
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    view = _visible(str(value))
    text = view.text.strip()
    cards = _cards(view)
    if _GROUPED_DIGITS.fullmatch(text) and (not view.soft or not cards):
        digits = "".join(_DIGITS.findall(text))
        return digits[-4:] if len(digits) >= 4 else None
    if len(cards) == 1 and len(cards[0][2]) == 1:
        begin, end, fours = cards[0]
        last4 = next(iter(fours))
        others = [
            g.group(0)
            for g in _DIGITS.finditer(view.text)
            if len(g.group(0)) >= 4 and (g.end() <= begin or g.start() >= end)
        ]
        return last4 if all(o[-4:] == last4 for o in others) else None
    long_groups = [g for g in _DIGITS.findall(view.text) if len(g) >= 4]
    return long_groups[0][-4:] if len(long_groups) == 1 else None


# What can sit between the digit groups of a printed card number: any run of
# whitespace (newlines and no-break, figure and em spaces included), or a
# hyphen, dash, minus, dot or slash with any whitespace around it.
_PUNCT = "-\u2010-\u2015\u2212./"
_SEPARATOR = rf"(?:\s+|\s*[{_PUNCT}]\s*)"
# A run of digit groups joined by separators: where a card number can sit
# among dates, prices, codes and other numbers.
_DIGIT_RUN = re.compile(rf"(?<![0-9])[0-9]+(?:{_SEPARATOR}[0-9]+)*(?![0-9])")
_DOT_OR_SLASH = re.compile(r"[./]")
# How a printed card number is grouped, by digits per group.
_CARD_SHAPES = frozenset({(4, 4, 4, 4), (4, 4, 4, 4, 3), (4, 6, 5), (4, 6, 4)})
# For each card length, the digit positions where its shapes have group
# boundaries.
_BOUNDARIES: dict[int, set[frozenset[int]]] = {}
for _shape in _CARD_SHAPES:
    _BOUNDARIES.setdefault(sum(_shape), set()).add(
        frozenset(sum(_shape[: k + 1]) for k in range(len(_shape) - 1))
    )
_REMOVED = "[card number removed]"


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _is_card(digits: str) -> bool:
    return 13 <= len(digits) <= 19 and len(set(digits)) > 1 and _luhn_valid(digits)


def _separator_kind(sep: str) -> str:
    """A dot or a slash is its own kind; whitespace and dashes are one kind.

    Spacing is ignored, since text from a PDF spaces unevenly.
    """
    found = _DOT_OR_SLASH.search(sep)
    return found.group(0) if found else " "


def _run_candidates(
    run: str, offset: int, soft: frozenset[int]
) -> list[tuple[int, int, str]]:
    """The card numbers in one digit run, as ``(start, end, last four)``.

    The run is cut into digit pieces at its separators and at soft breaks.
    A candidate is consecutive pieces holding 13 to 19 digits, not all one
    digit, that pass the Luhn check, and either have no separator between
    them (one unbroken number, soft breaks read as nothing), or fall into a
    card's shape (4-4-4-4, 4-4-4-4-3, 4-6-5, 4-6-4) with every separator on
    a group boundary, all of one kind (see ``_separator_kind``), and every
    other boundary on a soft break. Shapes keep dates, prices, phone and
    order numbers out; trying every starting piece finds a card with another
    number beside it.
    """
    pieces: list[tuple[int, int]] = []
    joins: list[str | None] = []  # between piece k and k+1; None is soft
    groups = list(_DIGITS.finditer(run))
    for g_index, group in enumerate(groups):
        if g_index:
            gap = run[groups[g_index - 1].end() : group.start()]
            joins.append(_separator_kind(gap))
        cuts = [
            cut for cut in range(group.start() + 1, group.end()) if offset + cut in soft
        ]
        # Dense soft breaks (obfuscated text: more than four, between more
        # than half of the digits) break nothing inside the dense stretch: it
        # is one number. A soft break beside an ordinary gap stays, since it
        # may be the only break between that number and another.
        if len(cuts) > 4 and len(cuts) * 2 > group.end() - group.start():
            cut_set = set(cuts)
            cuts = [
                cut
                for cut in cuts
                if (cut - 1 > group.start() and cut - 1 not in cut_set)
                or (cut + 1 < group.end() and cut + 1 not in cut_set)
            ]
        begin = group.start()
        for cut in cuts:
            pieces.append((begin, cut))
            joins.append(None)
            begin = cut
        pieces.append((begin, group.end()))

    found: list[tuple[int, int, str]] = []
    for i in range(len(pieces)):
        digits = ""
        real: dict[int, str] = {}
        softs: set[int] = set()
        for j in range(i, len(pieces)):
            if j > i:
                join = joins[j - 1]
                if join is None:
                    softs.add(len(digits))
                else:
                    real[len(digits)] = join
            digits += run[pieces[j][0] : pieces[j][1]]
            if len(digits) > 19:
                break
            if not _is_card(digits):
                continue
            fits = not real or any(
                set(real) <= bounds
                and bounds <= set(real) | softs
                and len(set(real.values())) == 1
                for bounds in _BOUNDARIES.get(len(digits), ())
            )
            if fits:
                found.append(
                    (offset + pieces[i][0], offset + pieces[j][1], digits[-4:])
                )
    return found


def _cards(view: _Visible) -> list[tuple[int, int, set[str]]]:
    """The card numbers in ``view.text``, as ``(start, end, last fours)``.

    Candidates that overlap are merged, so no part of a card is left: one
    replacement covers them all.
    """
    found: list[tuple[int, int, str]] = []
    for run in _DIGIT_RUN.finditer(view.text):
        found += _run_candidates(run.group(0), run.start(), view.soft)
    found.sort()
    merged: list[tuple[int, int, set[str]]] = []
    for begin, end, last4 in found:
        if merged and begin < merged[-1][1]:
            b, e, fours = merged[-1]
            if len(fours) < 2:  # only "one last four or not" matters
                fours.add(last4)
            merged[-1] = (b, max(e, end), fours)
        else:
            merged.append((begin, end, {last4}))
    return merged


def scrub_card_numbers(text: str) -> str:
    """``text`` with each card number replaced by "ending NNNN", its last four
    digits, or "[card number removed]" when overlapping candidates disagree
    on them. See ``_run_candidates`` for what counts as a card number; digits
    in any script count, and invisible characters may be nothing or a
    break."""
    view = _visible(text)
    out, pos = [], 0
    for begin, end, fours in _cards(view):
        first, last = view.index[begin], view.index[end - 1] + 1
        out.append(text[pos:first])
        out.append(f"ending {next(iter(fours))}" if len(fours) == 1 else _REMOVED)
        pos = last
    out.append(text[pos:])
    return "".join(out)


PaymentMethod = Literal["card", "bank", "wallet", "cash", "transfer", "other"]

# What a receipt is, which sets the sign of its amount in a total: a purchase
# counts for, a refund or credit against, and a statement not at all (its
# transactions are charged or receipted elsewhere).
Kind = Literal["purchase", "refund", "credit", "statement"]
KINDS: tuple[Kind, ...] = ("purchase", "refund", "credit", "statement")


class Payment(BaseModel):
    """How the receipt says it was paid, as printed on it."""

    method: PaymentMethod | None = None
    # Only ever the last four digits of the card or account.
    last4: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    # The cardholder or bill-to name as printed; not who forwarded the email.
    payer: str | None = None

    @field_validator("last4", mode="before")
    @classmethod
    def _last_four(cls, value: object) -> str | None:
        return last_four_digits(value)

    @field_validator("payer", mode="before")
    @classmethod
    def _payer(cls, value: object) -> object:
        # Free text, stored in the append-only history: no card numbers.
        value = _blank_to_none(value)
        return scrub_card_numbers(value) if isinstance(value, str) else value

    def is_empty(self) -> bool:
        return self.method is None and self.last4 is None and self.payer is None


class ReceiptMetadata(BaseModel):
    """Structured metadata extracted from a receipt by the LLM."""

    vendor: str = Field(min_length=1)
    # The order, invoice or statement total as the document states it.
    amount: Decimal = Field(ge=0)
    # What this transaction charged to the payer's payment methods (or, for a
    # refund, returned to them). None means not known; never filled from amount.
    paid_amount: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    date: date
    description: str | None = None
    # The order, confirmation or invoice number as printed. Not unique.
    order_number: str | None = None
    payment: Payment | None = None
    kind: Kind = "purchase"
    # Which part of an email held the receipt: an attachment's file name as
    # listed, or "body". None when the extractor can't tell, and always None
    # for a Drive document or a rendition.
    receipt_document: str | None = None
    # Whether this is a receipt at all; below 0.5 the item is skipped.
    confidence: float = Field(ge=0.0, le=1.0)
    field_confidence: FieldConfidence
    # The hosted receipt's URL when the receipt's details are only behind a
    # link. A low-confidence item with a link is recorded as a link-only skip.
    # Anything but an http(s) URL of up to 2048 characters becomes None, so a
    # bad link can't fail the extraction. Left out of model_dump, so the
    # append-only extraction history never holds a token it can't drop.
    receipt_link: str | None = Field(default=None, exclude=True)

    @field_validator("order_number", mode="before")
    @classmethod
    def _order_number(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("description", "vendor", mode="before")
    @classmethod
    def _no_card_numbers(cls, value: object) -> object:
        # The prompt asks for no more than the last four digits anywhere; this
        # holds whatever the model does in the free-text fields (payer too, in
        # Payment), since the extraction history that keeps the output is
        # append-only. order_number is left alone: real order numbers can be
        # long digit runs that pass the check.
        return scrub_card_numbers(value) if isinstance(value, str) else value

    @field_validator("payment", mode="after")
    @classmethod
    def _empty_payment_is_none(cls, value: Payment | None) -> Payment | None:
        return None if value is None or value.is_empty() else value

    @field_validator("receipt_link", mode="before")
    @classmethod
    def _http_link_or_none(cls, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        link = value.strip()
        if len(link) > MAX_RECEIPT_LINK_LENGTH:
            return None
        try:
            parts = urlsplit(link)
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        return link

    @model_validator(mode="wrap")
    @classmethod
    def _link_kept_out_of_errors(
        cls, data: Any, handler: ModelWrapValidatorHandler[ReceiptMetadata]
    ) -> ReceiptMetadata:
        # A validation error echoes its input, truncated in the middle, into
        # the traceback the pipeline logs; a token in the link's path would
        # survive that. So the other fields are validated without the link.
        if not isinstance(data, dict) or "receipt_link" not in data:
            return handler(data)
        rest = {k: v for k, v in data.items() if k != "receipt_link"}
        metadata = handler(rest)
        metadata.receipt_link = cls._http_link_or_none(data["receipt_link"])
        return metadata


# The longest receipt link kept; the ingest_log column has the same limit.
MAX_RECEIPT_LINK_LENGTH = 2048


def link_host(url: str) -> str:
    """A link's host name alone: safe to log, unlike its path and query."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    return host or "unknown host"


_URL = re.compile(r"https?://[^\s'\"<>\\]+", re.IGNORECASE)


def redact_links(text: str) -> str:
    """Every http(s) URL in ``text`` as its host alone."""
    return _URL.sub(lambda m: f"<link: {link_host(m.group(0))}>", text)


def redacted_traceback(exc: BaseException) -> str:
    """``exc``'s traceback, its cause chain included, with links redacted.

    A failed extraction's chain can hold the model's output, or the email
    text sent to it, and so a link whose path or query may carry a personal
    token. Log this instead of ``exc_info=True`` wherever that can be in the
    chain. It catches whole URLs only: pydantic truncates a long input in
    the middle, dropping the scheme, so ReceiptMetadata keeps receipt_link
    out of its validation errors itself.
    """
    return redact_links("".join(traceback.format_exception(exc)))


# Below this overall confidence an extraction isn't treated as a receipt:
# ingest skips the item, and re-extraction records the run and applies
# nothing (it is "gated"). One value for ingest, reextract and --needs-review.
MIN_RECEIPT_CONFIDENCE = 0.5

# An extraction run that failed records only this key in its output. Such a
# run never makes a receipt current, and --needs-review lists it.
FAILED_RUN_KEY = "error"


def receipt_values(metadata: ReceiptMetadata) -> dict[str, Any]:
    """The receipt fields an extraction gives, keyed by their applied name.

    Columns by name (``receipt_date`` for the model's ``date``), and each
    field confidence as ``field_confidence.<field>``: the names that
    ``extractions.applied_fields`` and ``receipts.manual_fields`` use.
    """
    payment = metadata.payment
    values: dict[str, Any] = {
        "vendor": metadata.vendor,
        "amount": metadata.amount,
        "paid_amount": metadata.paid_amount,
        "currency": metadata.currency,
        "receipt_date": metadata.date,
        "description": metadata.description,
        "order_number": metadata.order_number,
        "payment_method": payment.method if payment else None,
        "payment_last4": payment.last4 if payment else None,
        "payer": payment.payer if payment else None,
        "kind": metadata.kind,
        "confidence": metadata.confidence,
    }
    for name, value in metadata.field_confidence.model_dump().items():
        values[f"field_confidence.{name}"] = value
    return values


@dataclass
class ExtractionResult:
    """Metadata plus LLM usage from an extraction run."""

    metadata: ReceiptMetadata
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0


class ProvenanceSummary(BaseModel):
    """One source item of a receipt, as search and show report it."""

    id: UUID
    is_primary: bool
    source_type: str
    source_name: str
    source_id: str
    received_at: datetime
    email_subject: str | None = None
    email_sender: str | None = None
    file_name: str | None = None
    home_receipt_id: UUID | None = None
    match_method: str
    original_status: str


class Receipt(BaseModel):
    """Full receipt record as stored in the database."""

    id: UUID
    source_id: str
    source_type: str
    source_name: str
    vendor: str
    amount: Decimal
    paid_amount: Decimal | None = None
    currency: str
    receipt_date: date
    description: str | None
    confidence: float
    field_confidence: dict[str, float] = Field(default_factory=dict)
    # Fields a person has edited; re-extraction never writes them.
    manual_fields: list[str] = Field(default_factory=list)
    order_number: str | None = None
    payment_method: str | None = None
    payment_last4: str | None = None
    payer: str | None = None
    kind: Kind = "purchase"
    # The purchase this refund returns money for; only a refund has one.
    refund_of: UUID | None = None
    # From the v_receipt_net view, for a purchase read with it: the sum of its
    # linked refunds, and what it cost after them. None otherwise.
    refunded: Decimal | None = None
    net_paid: Decimal | None = None
    # Set when a duplicate merge emptied this receipt: the receipt that
    # remains (evidence-store design, §8). Merged receipts drop out of search.
    merged_into_id: UUID | None = None
    merged_at: datetime | None = None
    # Filled by search and get_receipt_by_id: the source items that evidence
    # this receipt (primary first), and the receipts it is flagged as a
    # possible duplicate of, with the pair still open.
    provenance: list[ProvenanceSummary] = Field(default_factory=list)
    suspected_duplicate_of: list[UUID] = Field(default_factory=list)
    pdf_path: str
    email_subject: str | None
    email_sender: str | None
    email_date: datetime | None
    file_name: str | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_amount(self) -> Decimal:
        """The paid amount when known, otherwise the total.

        Search filters on the same expression: COALESCE(paid_amount, amount).
        """
        return self.amount if self.paid_amount is None else self.paid_amount

    @computed_field  # type: ignore[prop-decorator]
    @property
    def signed_amount(self) -> Decimal:
        """The effective amount with the sign a total needs.

        A purchase counts for, a refund or credit against, and a statement
        counts nothing, so a plain sum is the net spend. The generated column
        receipts.signed_amount gives the same value in SQL.
        """
        if self.kind == "purchase":
            return self.effective_amount
        if self.kind == "statement":
            # 0.00, as the NUMERIC(12,2) column reads.
            return Decimal("0.00")
        # 0 - x, not -x: a refund of 0.00 (all store credit) stays 0.00,
        # where -Decimal("0.00") would print as -0.00.
        return Decimal(0) - self.effective_amount


EventType = Literal[
    "field_edited",
    "manual_cleared",
    "tag_added",
    "tag_removed",
    "link_added",
    "link_removed",
    "refund_linked",
    "refund_unlinked",
]
ActorKind = Literal["person", "agent", "rule"]


class ReceiptEvent(BaseModel):
    """One change made to a receipt after extraction (``receipt_events``).

    Append-only: who made the change (``actor``, and whether a person, an
    agent or a rule), what it was (``payload``) and why (``reason``).
    """

    id: UUID
    receipt_id: UUID
    event_type: EventType
    payload: dict[str, Any]
    actor: str
    actor_kind: ActorKind
    reason: str | None = None
    created_at: datetime


MatchMethod = Literal[
    "first", "original_hash", "document_hash", "order_number", "manual", "migrated"
]
OriginalStatus = Literal["stored", "pending", "unavailable"]


class Provenance(BaseModel):
    """One source item that evidences a receipt (``receipt.provenance``).

    An email, a forward or a Drive file. ``source_id`` is unique across every
    source, so an item already recorded here is never ingested again.
    """

    id: UUID
    receipt_id: UUID
    is_primary: bool
    source_type: str
    source_name: str
    source_id: str
    received_at: datetime
    email_subject: str | None = None
    email_sender: str | None = None
    email_date: datetime | None = None
    file_name: str | None = None
    source_sha256: bytes | None = None
    home_receipt_id: UUID | None = None
    original_file_id: UUID | None = None
    receipt_document_file_id: UUID | None = None
    document_certainty: Literal["certain", "ambiguous"] | None = None
    original_status: OriginalStatus
    original_unavailable_reason: str | None = None
    match_method: MatchMethod
    created_at: datetime
    updated_at: datetime


class IngestLogEntry(BaseModel):
    """A record of an ingest attempt (success, failure, or skip)."""

    id: UUID
    source_id: str
    source_type: str
    status: Literal["started", "success", "failed", "skipped", "queued"]
    receipt_id: UUID | None
    vendor: str | None
    amount: Decimal | None
    paid_amount: Decimal | None = None
    email_subject: str | None
    email_sender: str | None
    email_date: datetime | None
    error_message: str | None
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None
    llm_cache_read_tokens: int | None = None
    llm_requests: int | None = None
    llm_model: str | None = None
    source_name: str | None = None
    attempt: int = 1
    counts_attempt: bool = True
    next_retry_at: datetime | None = None
    error_class: Literal["transient", "content", "config"] | None = None
    stored_ref: str | None = None
    retry_request_id: UUID | None = None
    # A link-only skip's hosted receipt URL (may carry a personal token).
    receipt_link: str | None = None
    created_at: datetime
