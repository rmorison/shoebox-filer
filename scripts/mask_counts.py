#!/usr/bin/env python3
"""Count how the statement mask treats a set of statements: numbers only.

An owner step for the statement plan's mask trial (U5). It runs the mask
over every field it finds and prints how often each set-aside fires, how
often a year range or ZIP+4 is cut because of a neighbour, and how often
each side effect and known limit in the plan's Masking section occurs.

    uv run python scripts/mask_counts.py DIR

DIR is searched for:

- ``*.pdf``: read with the app's own statement reader, under the same
  bounds; each non-blank line of each page counts as one field. A page line
  stands in for an extracted description, which the model writes from it;
- ``*.txt``: each non-blank line is one field;
- ``*.json``: every string under a ``description``, ``section`` or
  ``institution`` key, at any depth (an extraction's output).

The output is fixed labels and counts, nothing else: no statement text, no
merchant, institution or file name, and no digit taken from the input. A
file that can't be read is counted, never named. Logging and warnings are
off, so no library prints a line of a statement either.

The counting mirrors the mask's own token rules (it calls the mask's
helpers) and checks itself: ``check.cut_count_mismatch`` counts fields
where its cuts differ from ``mask_digits``. It should be 0.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import warnings
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

HEADER = (
    "# statement mask counts (plan U5): fixed labels and counts only",
    "# effect.*, overcut.* and limit.* count shapes: upper bounds, since a",
    "# real account number can have the same shape as a side effect",
    "# label<TAB>count",
)

# Every label the script can print, in print order. Nothing else is printed.
LABELS: tuple[str, ...] = (
    "files.pdf",
    "files.txt",
    "files.json",
    "files.pdf_not_read",
    "files.failed",
    "files.other_type_skipped",
    "fields",
    "fields.with_a_digit_span",
    "fields.with_a_cut",
    "spans",
    "cuts",
    # How often each set-aside fires, by token and by field.
    "set_aside.amount_with_cents",
    "set_aside.short_date",
    "set_aside.short_date_range",
    "set_aside.year_range_alone",
    "set_aside.zip4_alone",
    "fields.with_set_aside.amount_with_cents",
    "fields.with_set_aside.short_date",
    "fields.with_set_aside.short_date_range",
    "fields.with_set_aside.year_range_alone",
    "fields.with_set_aside.zip4_alone",
    # A year range or ZIP+4 beside another digit group: cut with it.
    "neighbour.year_range_cut",
    "neighbour.zip4_cut",
    # Known side effects (plan, Masking: "Known side effects").
    "effect.plain_number_8_plus_digits_cut",
    "effect.phone_shape_cut",
    "effect.date_without_separators_cut",
    "effect.dashed_date_cut",
    "effect.date_dash_joined_to_number_cut",
    "effect.short_tokens_after_a_date_cut",
    "effect.adjacent_tokens_joined_cut",
    "effect.bare_thousands_over_nine_digits_cut",
    "overcut.two_years_side_by_side",
    "overcut.two_group_dash_number",
    "overcut.two_bare_thousands_side_by_side",
    "overcut.punctuation_after_year_range_or_zip4",
    "overcut.spaced_year_range",
    # Known limits that pass (plan, Masking: "Known limits").
    "limit.lone_bare_thousands_kept",
    "limit.date_shaped_group_before_a_cut",
    "limit.amount_right_after_a_cut",
    "check.cut_count_mismatch",
)

_JSON_KEYS = frozenset({"description", "section", "institution"})
_PHONE = re.compile(r"[0-9]{3}[-. ]+[0-9]{3}[-. ]+[0-9]{4}")
_DASHED_DATE = re.compile(
    r"(?:(?:19|20)[0-9]{2}-[0-9]{1,2}-[0-9]{1,2})"
    r"|(?:[0-9]{1,2}-[0-9]{1,2}-(?:[0-9]{2}|(?:19|20)[0-9]{2}))"
)
_TWO_GROUPS = re.compile(r"[0-9]{1,5}-[0-9]{1,5}")
_YEAR = re.compile(r"(?:19|20)[0-9]{2}")


def _valid_month_day(month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= 31


def _is_compact_date(core: str) -> bool:
    """``20261109``: eight digits that read as a year, month and day."""
    if len(core) != 8 or not core.isdigit():
        return False
    year, month, day = int(core[:4]), int(core[4:6]), int(core[6:])
    return 1900 <= year <= 2099 and _valid_month_day(month, day)


def _is_dashed_date(core: str) -> bool:
    if not _DASHED_DATE.fullmatch(core):
        return False
    parts = [int(p) for p in core.split("-")]
    if parts[0] >= 1900:  # year first
        return _valid_month_day(parts[1], parts[2])
    return _valid_month_day(parts[0], parts[1]) or _valid_month_day(parts[1], parts[0])


class _Mask:
    """The mask's own helpers, looked up once."""

    def __init__(self) -> None:
        from receipt_index import statement_extraction as se

        self.se = se

    def prepare(self, text: str) -> str:
        """The field as ``mask_digits`` sees it before reading spans."""
        se = self.se
        folded = se._fold_separators(se.normalise_statement_text(text))
        prepared: str = se._masked_run().sub("", folded)
        return prepared

    def digits(self, token: str) -> str:
        found: str = self.se._NON_DIGIT.sub("", token)
        return found

    def core(self, token: str) -> str:
        stripped: str = token.strip(self.se._JOINERS)
        return stripped

    def aside_kind(self, token: str, *, alone: bool) -> str | None:
        se = self.se
        if not se._set_aside(token, alone=alone):
            return None
        core = self.core(token)
        if se._is_cents_amount(core):
            return "amount_with_cents"
        if se._is_date(core):
            return "short_date_range" if "-" in core else "short_date"
        if se._is_year_range(core):
            return "year_range_alone"
        return "zip4_alone"

    def is_year_range(self, core: str) -> bool:
        return bool(self.se._is_year_range(core))

    def is_zip4(self, core: str) -> bool:
        return self.se._ZIP_PLUS_FOUR.fullmatch(core) is not None

    def is_bare_thousands(self, core: str) -> bool:
        return self.se._BARE_THOUSANDS.fullmatch(core) is not None

    def is_date(self, core: str) -> bool:
        """A short date or a range of two, by its shape."""
        return bool(self.se._is_date(core))

    def is_one_date(self, part: str) -> bool:
        return bool(self.se._is_one_date(part))


def count_field(mask: _Mask, text: str, counts: Counter[str]) -> None:
    """Add one field's counts. Reads the text; records numbers only."""
    se = mask.se
    counts["fields"] += 1
    prepared = mask.prepare(text)
    field_cuts = 0
    field_asides: set[str] = set()
    spans = list(se._SPAN.finditer(prepared))
    if spans:
        counts["fields.with_a_digit_span"] += 1
    for span_match in spans:
        counts["spans"] += 1
        span = span_match.group()
        field_cuts += _count_span(mask, span, counts, field_asides)
    if field_cuts:
        counts["fields.with_a_cut"] += 1
    for kind in field_asides:
        counts[f"fields.with_set_aside.{kind}"] += 1
    _, expected = se.mask_digits(text)
    if expected != field_cuts:
        counts["check.cut_count_mismatch"] += 1


def _count_span(
    mask: _Mask, span: str, counts: Counter[str], field_asides: set[str]
) -> int:
    se = mask.se
    tokens = list(se._TOKEN.finditer(span))
    left = [
        t
        for t in tokens
        if mask.digits(t.group()) and not se._set_aside(t.group(), alone=False)
    ]
    alone = len(left) == 1
    # The span as the mask reads it: set-aside tokens and runs between them.
    items: list[tuple[str, Any]] = []
    run: list[re.Match[str]] = []
    for token in tokens:
        kind = mask.aside_kind(token.group(), alone=alone)
        if kind is not None:
            if run:
                items.append(("run", run))
                run = []
            items.append(("aside", (kind, mask.core(token.group()))))
            counts[f"set_aside.{kind}"] += 1
            field_asides.add(kind)
        elif mask.digits(token.group()):
            run.append(token)
    if run:
        items.append(("run", run))

    cuts = 0
    for index, (what, value) in enumerate(items):
        if what != "run":
            continue
        before = items[index - 1][1] if index > 0 else None
        after = items[index + 1][1][0] if index + 1 < len(items) else None
        # Date-shaped by its look, whatever it was set aside as ("12.11"
        # and "11.09" read as amounts with cents first).
        after_date = before is not None and mask.is_date(before[1])
        if _count_run(mask, span, value, after_date=after_date, counts=counts):
            cuts += 1
            if after_date:
                counts["limit.date_shaped_group_before_a_cut"] += 1
            if after == "amount_with_cents":
                counts["limit.amount_right_after_a_cut"] += 1
    counts["cuts"] += cuts
    return cuts


def _count_run(
    mask: _Mask,
    span: str,
    run: list[re.Match[str]],
    *,
    after_date: bool,
    counts: Counter[str],
) -> bool:
    """Count one run's effects; True when the mask cuts it."""
    se = mask.se
    groups = [t.group() for t in run]
    cores = [mask.core(g) for g in groups]
    digits = "".join(mask.digits(g) for g in groups)
    lone_amount = (
        len(run) == 1
        and mask.is_bare_thousands(cores[0])
        and len(digits) <= se._MAX_BARE_AMOUNT_DIGITS
    )
    if len(digits) < se._MIN_MASKED_DIGITS:
        return False
    if lone_amount:
        counts["limit.lone_bare_thousands_kept"] += 1
        return False

    for group, core in zip(groups, cores, strict=True):
        if mask.is_year_range(core):
            counts["neighbour.year_range_cut"] += 1
        if mask.is_zip4(core):
            counts["neighbour.zip4_cut"] += 1
        if (mask.is_year_range(core) or mask.is_zip4(core)) and group != core:
            counts["overcut.punctuation_after_year_range_or_zip4"] += 1
        if _is_dashed_date(core):
            counts["effect.dashed_date_cut"] += 1
        parts = core.split("-")
        if len(parts) == 2 and any(mask.is_one_date(p) for p in parts):
            counts["effect.date_dash_joined_to_number_cut"] += 1

    text = span[run[0].start() : run[-1].end()]
    if len(digits) == 10 and _PHONE.fullmatch(text):
        counts["effect.phone_shape_cut"] += 1
    if len(run) == 1:
        core = cores[0]
        if core.isdigit():
            counts["effect.plain_number_8_plus_digits_cut"] += 1
            if _is_compact_date(core):
                counts["effect.date_without_separators_cut"] += 1
        if mask.is_bare_thousands(core):
            counts["effect.bare_thousands_over_nine_digits_cut"] += 1
        if (
            _TWO_GROUPS.fullmatch(core)
            and not mask.is_year_range(core)
            and not mask.is_zip4(core)
        ):
            counts["overcut.two_group_dash_number"] += 1
    else:
        counts["effect.adjacent_tokens_joined_cut"] += 1
        if after_date and all(len(mask.digits(g)) <= 4 for g in groups):
            counts["effect.short_tokens_after_a_date_cut"] += 1
        if all(mask.is_bare_thousands(c) for c in cores):
            counts["overcut.two_bare_thousands_side_by_side"] += 1
        if len(run) == 2 and all(_YEAR.fullmatch(c) for c in cores):
            between = span[run[0].end() : run[1].start()]
            if "-" in between:
                counts["overcut.spaced_year_range"] += 1
            else:
                counts["overcut.two_years_side_by_side"] += 1
    return True


def _json_fields(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _JSON_KEYS and isinstance(item, str):
                yield item
            else:
                yield from _json_fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_fields(item)


def _lines(text: str) -> Iterator[str]:
    for line in text.splitlines():
        if line.strip():
            yield line


def _pdf_fields(path: Path, counts: Counter[str]) -> Iterator[str]:
    from receipt_index.pdf_reader import read_statement
    from receipt_index.pdf_validate import STATEMENT_BOUNDS

    read = read_statement(path.read_bytes(), bounds=STATEMENT_BOUNDS)
    if read.page_texts is None:
        counts["files.pdf_not_read"] += 1
        return
    for page in read.page_texts:
        yield from _lines(page)


def _fields(path: Path, counts: Counter[str]) -> Iterator[str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        counts["files.pdf"] += 1
        yield from _pdf_fields(path, counts)
    elif suffix == ".txt":
        counts["files.txt"] += 1
        yield from _lines(path.read_text(encoding="utf-8", errors="replace"))
    elif suffix == ".json":
        counts["files.json"] += 1
        yield from _json_fields(json.loads(path.read_text(encoding="utf-8")))
    else:
        counts["files.other_type_skipped"] += 1


def count_paths(root: Path) -> Counter[str]:
    """Every count for the files under ``root`` (or ``root`` itself)."""
    mask = _Mask()
    counts: Counter[str] = Counter()
    paths = (
        [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    )
    for path in paths:
        # Collect the file's fields first: a file that fails part-way adds
        # no field counts, only files.failed.
        file_counts: Counter[str] = Counter()
        try:
            fields = list(_fields(path, file_counts))
            for field in fields:
                count_field(mask, field, file_counts)
        except Exception:
            counts["files.failed"] += 1
            continue
        counts.update(file_counts)
    return counts


def render(counts: Counter[str]) -> str:
    """Fixed labels and counts; a label the script doesn't know is a bug."""
    unknown = set(counts) - set(LABELS)
    if unknown:
        msg = "an unlisted label was counted"
        raise RuntimeError(msg)
    lines = [*HEADER, *(f"{label}\t{counts.get(label, 0)}" for label in LABELS)]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    if len(argv) != 1 or argv[0].startswith("-"):
        print("usage: mask_counts.py DIR", file=sys.stderr)
        return 2
    root = Path(argv[0])
    if not root.exists():
        # The path isn't echoed: it could name an institution.
        print("error: the path given doesn't exist", file=sys.stderr)
        return 2
    try:
        output = render(count_paths(root))
    except Exception:
        print("error: the count failed", file=sys.stderr)
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
