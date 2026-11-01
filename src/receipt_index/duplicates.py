"""Duplicate detection, link, unlink and merge (evidence-store design, §8).

A receipt is one purchase or refund; a provenance record is one source item
that evidences it. When the same receipt arrives twice, the second item's
provenance joins the first receipt, so totals count it once.

Rules, in order (rules 2 to 4 compare receipts of the same kind and currency;
every rule compares only receipts that aren't merged):

1. the whole original's sha256 equals an existing record's: link;
2. the receipt document's sha256 equals an existing record's, both certain:
   link when kind, vendor and effective amount match, else flag
   ``document_hash_conflict``;
3. the same order number, vendor, paid amount above zero and date: link;
   with only the date differing, within the window: flag
   ``order_number_conflict``;
4. the same vendor and effective amount, dates within the window: flag
   ``metadata``. Never link.

A merge keeps the receipt it empties (``merged_into_id``), and every link,
unlink, merge and unmerge is recorded in ``provenance_link_history``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

from receipt_index.models import Provenance, Receipt

if TYPE_CHECKING:
    from decimal import Decimal
    from uuid import UUID

    import psycopg

DEFAULT_WINDOW_DAYS = 3

LinkMethod = Literal["original_hash", "document_hash", "order_number", "manual"]
FlagReason = Literal[
    "metadata", "document_hash_conflict", "order_number_conflict", "merge_blocked"
]
ActorKind = Literal["system", "person", "agent"]
CandidateStatus = Literal["open", "linked", "dismissed"]

# The one vendor normalization: lower case, trimmed, whitespace runs collapsed.
_NORM = "lower(regexp_replace(btrim({}), '\\s+', ' ', 'g'))"

# A provenance record's receipt document hash, where it takes part in rule 2:
# its certain receipt document's, or, for a Drive file not yet adopted (no
# receipt document recorded), the file's own hash (spec drift a).
_DOC_HASH = """\
CASE
    WHEN p.document_certainty = 'certain' THEN d.sha256
    WHEN p.source_type = 'gdrive' AND p.receipt_document_file_id IS NULL
        THEN p.source_sha256
END"""


class DuplicateError(Exception):
    """A link, unlink or decision that can't be made; the message says why."""


class MergeBlockedError(DuplicateError):
    """A merge refused because of what the receipt it would empty carries."""


@dataclass(frozen=True)
class Actor:
    """Who made a change, as the history records it."""

    name: str
    kind: ActorKind
    reason: str | None = None


INGEST = Actor("ingest", "system")


@dataclass(frozen=True)
class ItemFacts:
    """What the rules compare about one side of a pair.

    ``source_hashes`` and ``doc_hashes`` are the item's whole-original and
    receipt-document hashes (several for a receipt the scan compares, one
    per provenance record). ``before`` limits the matches to receipts created
    before it (the scan compares each receipt with older ones only), and
    ``exclude`` leaves out the receipt itself.
    """

    kind: str
    currency: str
    vendor: str
    effective_amount: Decimal
    paid_amount: Decimal | None
    order_number: str | None
    receipt_date: date
    source_hashes: tuple[bytes, ...] = ()
    doc_hashes: tuple[bytes, ...] = ()
    exclude: UUID | None = None
    before: tuple[datetime, UUID] | None = None
    # Receipts a person kept apart from this one: the rules skip them.
    kept_apart: tuple[UUID, ...] = ()


@dataclass
class Decision:
    """What the rules decided: a receipt to join, and pairs to flag."""

    join: UUID | None = None
    method: LinkMethod | None = None
    # (receipt ID, reason), one per receipt, in the order found.
    flags: list[tuple[UUID, FlagReason]] = field(default_factory=list)

    def flag(self, receipt_id: UUID, reason: FlagReason) -> None:
        """Flag ``receipt_id`` once, never the receipt the item joins."""
        if receipt_id != self.join and all(r != receipt_id for r, _ in self.flags):
            self.flags.append((receipt_id, reason))


def _scope(
    params: dict[str, object],
    exclude: UUID | None,
    before: tuple[datetime, UUID] | None,
    kept: tuple[UUID, ...] = (),
) -> str:
    """The live receipts (aliased r) an item may match."""
    clauses = ["r.merged_into_id IS NULL"]
    if exclude is not None:
        clauses.append("r.id <> %(exclude)s")
        params["exclude"] = exclude
    if kept:
        clauses.append("NOT (r.id = ANY(%(kept)s))")
        params["kept"] = list(kept)
    if before is not None:
        clauses.append("(r.created_at, r.id) < (%(before_at)s, %(before_id)s)")
        params["before_at"], params["before_id"] = before
    return " AND ".join(clauses)


_OLDEST = "ORDER BY r.created_at, r.id"


def match_original(
    conn: psycopg.Connection[dict[str, Any]],
    hashes: tuple[bytes, ...],
    *,
    exclude: UUID | None = None,
    before: tuple[datetime, UUID] | None = None,
    kept: tuple[UUID, ...] = (),
) -> UUID | None:
    """Rule 1: the oldest live receipt holding an item with identical bytes.

    Needs no extraction, so ingest runs it before the LLM call.
    """
    if not hashes:
        return None
    params: dict[str, object] = {"hashes": list(hashes)}
    row = conn.execute(
        f"""\
        SELECT r.id FROM receipt.provenance p
        JOIN receipt.receipts r ON r.id = p.receipt_id
        WHERE p.source_sha256 = ANY(%(hashes)s)
          AND {_scope(params, exclude, before, kept)}
        {_OLDEST} LIMIT 1
        """,
        params,
    ).fetchone()
    return row["id"] if row else None


def decide(
    conn: psycopg.Connection[dict[str, Any]],
    facts: ItemFacts,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Decision:
    """Run rules 1 to 4 for one item against the receipts in scope."""
    decision = Decision()
    original = match_original(
        conn,
        facts.source_hashes,
        exclude=facts.exclude,
        before=facts.before,
        kept=facts.kept_apart,
    )
    if original is not None:
        decision.join, decision.method = original, "original_hash"
        return decision

    base: dict[str, object] = {
        "kind": facts.kind,
        "currency": facts.currency,
        "vendor": facts.vendor,
        "effective": facts.effective_amount,
        "paid": facts.paid_amount,
        "order_number": facts.order_number,
        "date": facts.receipt_date,
        "window": window_days,
    }
    same_receipt = (
        f"r.kind = %(kind)s AND r.currency = %(currency)s"
        f" AND {_NORM.format('r.vendor')} = {_NORM.format('%(vendor)s')}"
    )

    # Rule 2, with its guard.
    if facts.doc_hashes:
        params = {**base, "docs": list(facts.doc_hashes)}
        rows = conn.execute(
            f"""\
            SELECT DISTINCT r.id, r.created_at,
                   ({same_receipt}
                    AND COALESCE(r.paid_amount, r.amount) = %(effective)s) AS guarded
            FROM receipt.provenance p
            JOIN receipt.receipts r ON r.id = p.receipt_id
            LEFT JOIN receipt.stored_files d ON d.id = p.receipt_document_file_id
            WHERE ({_DOC_HASH}) = ANY(%(docs)s)
              AND {_scope(params, facts.exclude, facts.before, facts.kept_apart)}
            ORDER BY r.created_at, r.id
            """,
            params,
        ).fetchall()
        for row in rows:
            if row["guarded"] and decision.join is None:
                decision.join, decision.method = row["id"], "document_hash"
        if decision.join is not None:
            return decision
        for row in rows:
            decision.flag(row["id"], "document_hash_conflict")

    # Rule 3: a paid amount above zero on both sides, or nothing at all.
    if (
        facts.order_number is not None
        and facts.paid_amount is not None
        and facts.paid_amount > 0
    ):
        params = dict(base)
        rows = conn.execute(
            f"""\
            SELECT r.id, r.receipt_date = %(date)s AS same_date
            FROM receipt.receipts r
            WHERE {same_receipt} AND r.order_number = %(order_number)s
              AND r.paid_amount = %(paid)s AND r.paid_amount > 0
              AND abs(r.receipt_date - %(date)s::date) <= %(window)s
              AND {_scope(params, facts.exclude, facts.before, facts.kept_apart)}
            {_OLDEST}
            """,
            params,
        ).fetchall()
        for row in rows:
            if row["same_date"]:
                decision.join, decision.method = row["id"], "order_number"
                decision.flags = [f for f in decision.flags if f[0] != row["id"]]
                return decision
        for row in rows:
            decision.flag(row["id"], "order_number_conflict")

    # Rule 4: flag only.
    params = dict(base)
    rows = conn.execute(
        f"""\
        SELECT r.id FROM receipt.receipts r
        WHERE {same_receipt}
          AND COALESCE(r.paid_amount, r.amount) = %(effective)s
          AND abs(r.receipt_date - %(date)s::date) <= %(window)s
          AND {_scope(params, facts.exclude, facts.before, facts.kept_apart)}
        {_OLDEST}
        """,
        params,
    ).fetchall()
    for row in rows:
        decision.flag(row["id"], "metadata")
    return decision


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def primary_provenance(
    conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID
) -> UUID | None:
    """The ID of the receipt's primary provenance record, or None."""
    row = conn.execute(
        "SELECT id FROM receipt.provenance WHERE receipt_id = %s AND is_primary",
        (receipt_id,),
    ).fetchone()
    return row["id"] if row else None


def insert_candidate(
    conn: psycopg.Connection[dict[str, Any]],
    a: UUID,
    b: UUID,
    reason: FlagReason,
) -> bool:
    """Flag the pair (a, b) unless it is already recorded, in either order.

    The newer record (by ``created_at``, then ``id``) is ``provenance_id``.
    Returns whether a row was written. Doesn't commit.
    """
    row = conn.execute(
        """\
        WITH pair AS (
            SELECT p.id, p.created_at FROM receipt.provenance p
            WHERE p.id IN (%(a)s, %(b)s)
        ), ordered AS (
            SELECT (SELECT id FROM pair ORDER BY created_at DESC, id DESC LIMIT 1)
                       AS newer,
                   (SELECT id FROM pair ORDER BY created_at, id LIMIT 1) AS older
        )
        INSERT INTO receipt.duplicate_candidates
            (provenance_id, candidate_of_provenance_id, reason)
        SELECT newer, older, %(reason)s FROM ordered
        WHERE newer <> older AND NOT EXISTS (
            SELECT 1 FROM receipt.duplicate_candidates c
            WHERE (c.provenance_id, c.candidate_of_provenance_id)
                  IN ((%(a)s, %(b)s), (%(b)s, %(a)s))
        )
        ON CONFLICT ON CONSTRAINT uq_duplicate_candidates_pair DO NOTHING
        RETURNING id
        """,
        {"a": a, "b": b, "reason": reason},
    ).fetchone()
    return row is not None


def record_flags(
    conn: psycopg.Connection[dict[str, Any]],
    provenance_id: UUID,
    flags: list[tuple[UUID, FlagReason]],
) -> int:
    """Flag a new item's record against each flagged receipt's primary."""
    written = 0
    for receipt_id, reason in flags:
        other = primary_provenance(conn, receipt_id)
        if other is not None and insert_candidate(conn, provenance_id, other, reason):
            written += 1
    return written


@dataclass(frozen=True)
class Candidate:
    """A flagged pair, with the receipts each record is on now."""

    id: UUID
    provenance_id: UUID
    candidate_of_provenance_id: UUID
    reason: str
    status: CandidateStatus
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime
    receipt: Receipt
    candidate_of_receipt: Receipt


def list_candidates(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    status: CandidateStatus | None = "open",
    receipt_id: UUID | None = None,
) -> list[Candidate]:
    """Flagged pairs, newest first, each with both current receipts.

    ``status`` None lists every status. ``receipt_id`` keeps the pairs with
    a record on that receipt.
    """
    clauses = ["TRUE"]
    params: dict[str, object] = {}
    if status is not None:
        clauses.append("c.status = %(status)s")
        params["status"] = status
    if receipt_id is not None:
        clauses.append("(pa.receipt_id = %(rid)s OR pb.receipt_id = %(rid)s)")
        params["rid"] = receipt_id
    rows = conn.execute(
        f"""\
        SELECT c.*, pa.receipt_id AS a_receipt, pb.receipt_id AS b_receipt
        FROM receipt.duplicate_candidates c
        JOIN receipt.provenance pa ON pa.id = c.provenance_id
        JOIN receipt.provenance pb ON pb.id = c.candidate_of_provenance_id
        WHERE {" AND ".join(clauses)}
        ORDER BY c.created_at DESC, c.id DESC
        """,
        params,
    ).fetchall()
    ids = {row["a_receipt"] for row in rows} | {row["b_receipt"] for row in rows}
    receipts = _receipts(conn, ids)
    return [
        Candidate(
            id=row["id"],
            provenance_id=row["provenance_id"],
            candidate_of_provenance_id=row["candidate_of_provenance_id"],
            reason=row["reason"],
            status=row["status"],
            decided_by=row["decided_by"],
            decided_at=row["decided_at"],
            created_at=row["created_at"],
            receipt=receipts[row["a_receipt"]],
            candidate_of_receipt=receipts[row["b_receipt"]],
        )
        for row in rows
    ]


def _receipts(
    conn: psycopg.Connection[dict[str, Any]], ids: set[UUID]
) -> dict[UUID, Receipt]:
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT * FROM receipt.receipts WHERE id = ANY(%s)", (list(ids),)
    ).fetchall()
    return {row["id"]: Receipt.model_validate(row) for row in rows}


def suspected_duplicates(
    conn: psycopg.Connection[dict[str, Any]], receipt_ids: list[UUID]
) -> dict[UUID, list[UUID]]:
    """For each receipt, the other receipts it has open candidate pairs with."""
    if not receipt_ids:
        return {}
    rows = conn.execute(
        """\
        SELECT pa.receipt_id AS a, pb.receipt_id AS b
        FROM receipt.duplicate_candidates c
        JOIN receipt.provenance pa ON pa.id = c.provenance_id
        JOIN receipt.provenance pb ON pb.id = c.candidate_of_provenance_id
        WHERE c.status = 'open' AND pa.receipt_id <> pb.receipt_id
          AND (pa.receipt_id = ANY(%(ids)s) OR pb.receipt_id = ANY(%(ids)s))
        ORDER BY c.created_at, c.id
        """,
        {"ids": receipt_ids},
    ).fetchall()
    out: dict[UUID, list[UUID]] = {rid: [] for rid in receipt_ids}
    for row in rows:
        for this, other in ((row["a"], row["b"]), (row["b"], row["a"])):
            if this in out and other not in out[this]:
                out[this].append(other)
    return out


def provenance_for(
    conn: psycopg.Connection[dict[str, Any]], receipt_ids: list[UUID]
) -> dict[UUID, list[Provenance]]:
    """Each receipt's provenance records, primary first, then oldest first."""
    if not receipt_ids:
        return {}
    rows = conn.execute(
        """\
        SELECT * FROM receipt.provenance WHERE receipt_id = ANY(%s)
        ORDER BY is_primary DESC, created_at, id
        """,
        (receipt_ids,),
    ).fetchall()
    out: dict[UUID, list[Provenance]] = {rid: [] for rid in receipt_ids}
    for row in rows:
        out[row["receipt_id"]].append(Provenance.model_validate(row))
    return out


def merged_into_this(
    conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID
) -> list[UUID]:
    """The receipts merged into this one, oldest merge first."""
    rows = conn.execute(
        "SELECT id FROM receipt.receipts WHERE merged_into_id = %s"
        " ORDER BY merged_at, id",
        (receipt_id,),
    ).fetchall()
    return [row["id"] for row in rows]


# ---------------------------------------------------------------------------
# Link, merge, unlink
# ---------------------------------------------------------------------------


def _history(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    provenance_id: UUID,
    action: str,
    from_receipt_id: UUID | None,
    to_receipt_id: UUID,
    match_method: str,
    actor: Actor,
) -> None:
    conn.execute(
        """\
        INSERT INTO receipt.provenance_link_history (
            provenance_id, action, from_receipt_id, to_receipt_id,
            match_method, actor, actor_kind, reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            provenance_id,
            action,
            from_receipt_id,
            to_receipt_id,
            match_method,
            actor.name,
            actor.kind,
            actor.reason,
        ),
    )


def _lock_receipt(
    conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID
) -> Receipt:
    row = conn.execute(
        "SELECT * FROM receipt.receipts WHERE id = %s FOR UPDATE", (receipt_id,)
    ).fetchone()
    if row is None:
        msg = f"no receipt {receipt_id}"
        raise DuplicateError(msg)
    return Receipt.model_validate(row)


def lock_target(conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID) -> Receipt:
    """Lock the receipt a link goes to: the one that remains, if it was merged."""
    receipt = _lock_receipt(conn, receipt_id)
    if receipt.merged_into_id is not None:
        receipt = _lock_receipt(conn, receipt.merged_into_id)
        if receipt.merged_into_id is not None:  # the trigger keeps this from happening
            msg = f"receipt {receipt.id} is merged into a merged receipt"
            raise DuplicateError(msg)
    return receipt


def _lock_provenance(
    conn: psycopg.Connection[dict[str, Any]], provenance_id: UUID
) -> Provenance:
    row = conn.execute(
        "SELECT * FROM receipt.provenance WHERE id = %s FOR UPDATE", (provenance_id,)
    ).fetchone()
    if row is None:
        msg = f"no provenance record {provenance_id}"
        raise DuplicateError(msg)
    return Provenance.model_validate(row)


def _count_on(conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM receipt.provenance WHERE receipt_id = %s",
        (receipt_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def merge_blocker(emptied: Receipt, target: Receipt) -> str | None:
    """Why ``emptied`` can't be merged into ``target``, or None.

    Hand edits would drop out of search with the merged receipt, so a person
    moves or removes them first. A refund linked to a purchase the other
    refund isn't linked to would drop out of that purchase's net paid.
    """
    if emptied.kind != target.kind:
        return f"receipt {emptied.id} is a {emptied.kind}, {target.id} a {target.kind}"
    if emptied.manual_fields:
        fields = ", ".join(sorted(emptied.manual_fields))
        return f"receipt {emptied.id} has hand-edited fields ({fields})"
    if emptied.refund_of is not None and emptied.refund_of != target.refund_of:
        return (
            f"refund {emptied.id} is linked to purchase {emptied.refund_of},"
            f" and {target.id} isn't"
        )
    return None


def _promote_oldest(conn: psycopg.Connection[dict[str, Any]], receipt_id: UUID) -> None:
    conn.execute(
        """\
        UPDATE receipt.provenance SET is_primary = TRUE
        WHERE id = (
            SELECT id FROM receipt.provenance WHERE receipt_id = %(r)s
            ORDER BY created_at, id LIMIT 1
        ) AND NOT EXISTS (
            SELECT 1 FROM receipt.provenance WHERE receipt_id = %(r)s AND is_primary
        )
        """,
        {"r": receipt_id},
    )


def _move(
    conn: psycopg.Connection[dict[str, Any]],
    prov: Provenance,
    to: UUID,
    *,
    primary: bool,
    method: str,
    home: UUID | None = None,
) -> None:
    """Move a record to receipt ``to``; the receipt it left keeps a primary."""
    conn.execute(
        """\
        UPDATE receipt.provenance
        SET receipt_id = %(to)s, is_primary = %(primary)s, match_method = %(method)s,
            home_receipt_id = COALESCE(%(home)s, home_receipt_id)
        WHERE id = %(id)s
        """,
        {"to": to, "primary": primary, "method": method, "home": home, "id": prov.id},
    )
    _promote_oldest(conn, prov.receipt_id)


def _merge(
    conn: psycopg.Connection[dict[str, Any]],
    emptied: Receipt,
    target: Receipt,
    *,
    provenance_id: UUID,
    method: str,
    actor: Actor,
) -> None:
    """Mark ``emptied`` merged into ``target``, flattening any chain into it."""
    for chained in merged_into_this(conn, emptied.id):
        conn.execute(
            "UPDATE receipt.receipts SET merged_into_id = %s WHERE id = %s",
            (target.id, chained),
        )
        _history(
            conn,
            provenance_id=provenance_id,
            action="merge",
            from_receipt_id=emptied.id,
            to_receipt_id=target.id,
            match_method=method,
            actor=actor,
        )
    conn.execute(
        "UPDATE receipt.receipts SET merged_into_id = %s, merged_at = %s WHERE id = %s",
        (target.id, datetime.now(UTC), emptied.id),
    )
    _history(
        conn,
        provenance_id=provenance_id,
        action="merge",
        from_receipt_id=emptied.id,
        to_receipt_id=target.id,
        match_method=method,
        actor=actor,
    )


def link(
    conn: psycopg.Connection[dict[str, Any]],
    provenance_id: UUID,
    receipt_id: UUID,
    *,
    actor: Actor,
    method: LinkMethod = "manual",
) -> UUID:
    """Move a provenance record to a receipt (the one that remains).

    If it was the last record on its receipt, that receipt is merged, unless
    ``merge_blocker`` refuses. Doesn't commit. Returns the receipt linked to.
    """
    prov = _lock_provenance(conn, provenance_id)
    source = _lock_receipt(conn, prov.receipt_id)
    target = lock_target(conn, receipt_id)
    if source.id == target.id:
        msg = f"provenance {provenance_id} is already on receipt {target.id}"
        raise DuplicateError(msg)
    empties = _count_on(conn, source.id) == 1
    if empties:
        blocker = merge_blocker(source, target)
        if blocker is not None:
            raise MergeBlockedError(blocker)
    elif source.kind != target.kind:
        msg = f"receipt {source.id} is a {source.kind}, {target.id} a {target.kind}"
        raise DuplicateError(msg)
    _move(conn, prov, target.id, primary=False, method=method)
    _history(
        conn,
        provenance_id=prov.id,
        action="link",
        from_receipt_id=source.id,
        to_receipt_id=target.id,
        match_method=method,
        actor=actor,
    )
    if empties:
        _merge(conn, source, target, provenance_id=prov.id, method=method, actor=actor)
    return target.id


def merge_receipt(
    conn: psycopg.Connection[dict[str, Any]],
    emptied_id: UUID,
    target_id: UUID,
    *,
    actor: Actor,
    method: LinkMethod,
) -> UUID:
    """Move every record of one receipt to another, merging it. Doesn't commit."""
    emptied = _lock_receipt(conn, emptied_id)
    target = lock_target(conn, target_id)
    if emptied.merged_into_id is not None:
        msg = f"receipt {emptied_id} is already merged into {emptied.merged_into_id}"
        raise DuplicateError(msg)
    if emptied.id == target.id:
        msg = f"receipt {emptied_id} is the receipt that remains"
        raise DuplicateError(msg)
    blocker = merge_blocker(emptied, target)
    if blocker is not None:
        raise MergeBlockedError(blocker)
    rows = conn.execute(
        "SELECT id FROM receipt.provenance WHERE receipt_id = %s"
        " ORDER BY is_primary, created_at, id",
        (emptied.id,),
    ).fetchall()
    for row in rows:
        link(conn, row["id"], target.id, actor=actor, method=method)
    return target.id


@dataclass(frozen=True)
class UnlinkResult:
    """Where an unlinked record went, and whether its home was restored."""

    receipt_id: UUID
    restored: bool


def unlink_home(
    conn: psycopg.Connection[dict[str, Any]],
    provenance_id: UUID,
    *,
    actor: Actor,
) -> UnlinkResult | None:
    """Move a record back to its home receipt, restoring it if it was merged.

    Returns None when the record has no home receipt (it joined at ingest),
    for the caller to give it a new receipt. Doesn't commit.
    """
    prov = _lock_provenance(conn, provenance_id)
    current = _lock_receipt(conn, prov.receipt_id)
    if prov.home_receipt_id is None:
        _check_not_last(conn, prov)
        return None
    if prov.home_receipt_id == current.id:
        msg = (
            f"provenance {provenance_id} is on its own receipt {current.id};"
            " unlink the other record instead"
        )
        raise DuplicateError(msg)
    _check_not_last(conn, prov)
    home = _lock_receipt(conn, prov.home_receipt_id)
    restored = home.merged_into_id is not None
    if restored:
        conn.execute(
            "UPDATE receipt.receipts SET merged_into_id = NULL, merged_at = NULL"
            " WHERE id = %s",
            (home.id,),
        )
    _move(conn, prov, home.id, primary=restored, method="manual")
    _history(
        conn,
        provenance_id=prov.id,
        action="unlink",
        from_receipt_id=current.id,
        to_receipt_id=home.id,
        match_method="manual",
        actor=actor,
    )
    if restored:
        _history(
            conn,
            provenance_id=prov.id,
            action="unmerge",
            from_receipt_id=current.id,
            to_receipt_id=home.id,
            match_method="manual",
            actor=actor,
        )
    return UnlinkResult(home.id, restored)


def _check_not_last(conn: psycopg.Connection[dict[str, Any]], prov: Provenance) -> None:
    if _count_on(conn, prov.receipt_id) == 1:
        msg = (
            f"provenance {prov.id} is the only record on receipt {prov.receipt_id};"
            " there is nothing to separate it from"
        )
        raise DuplicateError(msg)


def unlink_to_new(
    conn: psycopg.Connection[dict[str, Any]],
    provenance_id: UUID,
    new_receipt_id: UUID,
    *,
    actor: Actor,
) -> UnlinkResult:
    """Move a record with no home to the new receipt made for it, as its home.

    The caller has created ``new_receipt_id`` from the record's own item.
    Doesn't commit.
    """
    prov = _lock_provenance(conn, provenance_id)
    current = prov.receipt_id
    _check_not_last(conn, prov)
    _move(
        conn, prov, new_receipt_id, primary=True, method="manual", home=new_receipt_id
    )
    _history(
        conn,
        provenance_id=prov.id,
        action="unlink",
        from_receipt_id=current,
        to_receipt_id=new_receipt_id,
        match_method="manual",
        actor=actor,
    )
    return UnlinkResult(new_receipt_id, restored=False)


def join_at_ingest(
    conn: psycopg.Connection[dict[str, Any]],
    provenance_id: UUID,
    receipt_id: UUID,
    method: LinkMethod,
) -> None:
    """Record a new item's join in the history (its row is already there)."""
    _history(
        conn,
        provenance_id=provenance_id,
        action="link",
        from_receipt_id=None,
        to_receipt_id=receipt_id,
        match_method=method,
        actor=INGEST,
    )


def decide_candidate(
    conn: psycopg.Connection[dict[str, Any]],
    candidate_id: UUID,
    status: Literal["linked", "dismissed"],
    *,
    actor: Actor,
) -> UUID | None:
    """Link or dismiss an open pair. Linking merges the newer receipt into the
    older (the scan's survivor rule). Returns the receipt that remains, if
    linked. Doesn't commit.
    """
    row = conn.execute(
        """\
        SELECT c.status, pa.receipt_id AS a, pb.receipt_id AS b
        FROM receipt.duplicate_candidates c
        JOIN receipt.provenance pa ON pa.id = c.provenance_id
        JOIN receipt.provenance pb ON pb.id = c.candidate_of_provenance_id
        WHERE c.id = %s FOR UPDATE OF c
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        msg = f"no duplicate pair {candidate_id}"
        raise DuplicateError(msg)
    if row["status"] != "open":
        msg = f"pair {candidate_id} is already {row['status']}"
        raise DuplicateError(msg)
    remaining: UUID | None = None
    if status == "linked":
        a, b = _lock_receipt(conn, row["a"]), _lock_receipt(conn, row["b"])
        if a.id == b.id:
            remaining = a.id
        else:
            older, newer = sorted((a, b), key=lambda r: (r.created_at, r.id))
            remaining = merge_receipt(
                conn, newer.id, older.id, actor=actor, method="manual"
            )
    conn.execute(
        """\
        UPDATE receipt.duplicate_candidates
        SET status = %s, decided_by = %s, decided_at = %s
        WHERE id = %s
        """,
        (status, actor.name, datetime.now(UTC), candidate_id),
    )
    return remaining


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

SCAN = Actor("duplicates-scan", "system", "duplicates scan")


def kept_apart(conn: psycopg.Connection[dict[str, Any]], a: UUID, b: UUID) -> bool:
    """Whether a person has decided receipts ``a`` and ``b`` are two receipts.

    An unlink between them (either way, counting the receipts merged into
    each), or a dismissed pair between their records. Such a decision
    outranks the rules: the scan never links the pair again. A later hand
    ``link`` doesn't cancel an earlier unlink, so after an unlink only a
    person re-merges the pair.
    """
    row = conn.execute(
        """\
        WITH fa AS (
            SELECT id FROM receipt.receipts WHERE id = %(a)s OR merged_into_id = %(a)s
        ), fb AS (
            SELECT id FROM receipt.receipts WHERE id = %(b)s OR merged_into_id = %(b)s
        )
        SELECT EXISTS (
            SELECT 1 FROM receipt.provenance_link_history h
            WHERE h.action = 'unlink' AND (
                (h.from_receipt_id IN (SELECT id FROM fa)
                 AND h.to_receipt_id IN (SELECT id FROM fb))
                OR (h.from_receipt_id IN (SELECT id FROM fb)
                    AND h.to_receipt_id IN (SELECT id FROM fa))
            )
        ) OR EXISTS (
            SELECT 1 FROM receipt.duplicate_candidates c
            JOIN receipt.provenance pa ON pa.id = c.provenance_id
            JOIN receipt.provenance pb ON pb.id = c.candidate_of_provenance_id
            WHERE c.status = 'dismissed' AND (
                (pa.receipt_id = %(a)s AND pb.receipt_id = %(b)s)
                OR (pa.receipt_id = %(b)s AND pb.receipt_id = %(a)s)
            )
        ) AS apart
        """,
        {"a": a, "b": b},
    ).fetchone()
    return bool(row and row["apart"])


@dataclass
class ScanResult:
    """What a scan linked and flagged, counted per rule and reason."""

    linked: dict[str, int] = field(default_factory=dict)
    flagged: dict[str, int] = field(default_factory=dict)
    # (merged receipt, receipt that remains, method)
    links: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    # (receipt, other receipt, reason)
    flags: list[tuple[UUID, UUID, str]] = field(default_factory=list)


def _facts_for(conn: psycopg.Connection[dict[str, Any]], receipt: Receipt) -> ItemFacts:
    rows = conn.execute(
        f"""\
        SELECT p.source_sha256, ({_DOC_HASH}) AS doc
        FROM receipt.provenance p
        LEFT JOIN receipt.stored_files d ON d.id = p.receipt_document_file_id
        WHERE p.receipt_id = %s
        """,
        (receipt.id,),
    ).fetchall()
    return ItemFacts(
        kind=receipt.kind,
        currency=receipt.currency,
        vendor=receipt.vendor,
        effective_amount=receipt.effective_amount,
        paid_amount=receipt.paid_amount,
        order_number=receipt.order_number,
        receipt_date=receipt.receipt_date,
        source_hashes=tuple(r["source_sha256"] for r in rows if r["source_sha256"]),
        doc_hashes=tuple(r["doc"] for r in rows if r["doc"]),
        exclude=receipt.id,
        before=(receipt.created_at, receipt.id),
    )


def scan(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> ScanResult:
    """Run rules 1 to 4 over existing receipts, oldest first. Doesn't commit.

    Each receipt is compared with the live receipts created before it, so the
    older receipt of a pair is the one that remains. A merge the guard
    refuses is flagged ``merge_blocked``. A pair a person unlinked or
    dismissed (``kept_apart``) is never linked again. The caller commits, or
    rolls back for a dry run.
    """
    result = ScanResult()
    rows = conn.execute(
        "SELECT * FROM receipt.receipts WHERE merged_into_id IS NULL"
        " ORDER BY created_at, id"
    ).fetchall()
    for row in rows:
        receipt = Receipt.model_validate(row)
        current = conn.execute(
            "SELECT merged_into_id FROM receipt.receipts WHERE id = %s", (receipt.id,)
        ).fetchone()
        if current is None or current["merged_into_id"] is not None:
            continue
        facts = _facts_for(conn, receipt)
        decision = decide(conn, facts, window_days=window_days)
        # A receipt a person kept apart from this one drops out, and the
        # rules run again without it, so its other matches still count.
        while decision.join is not None and kept_apart(conn, receipt.id, decision.join):
            facts = replace(facts, kept_apart=(*facts.kept_apart, decision.join))
            decision = decide(conn, facts, window_days=window_days)
        mine = primary_provenance(conn, receipt.id)
        if decision.join is not None and decision.method is not None:
            conn.execute("SAVEPOINT scan_merge")
            try:
                merge_receipt(
                    conn,
                    receipt.id,
                    decision.join,
                    actor=SCAN,
                    method=decision.method,
                )
            except MergeBlockedError:
                conn.execute("ROLLBACK TO SAVEPOINT scan_merge")
                decision.flags.insert(0, (decision.join, "merge_blocked"))
            else:
                conn.execute("RELEASE SAVEPOINT scan_merge")
                result.linked[decision.method] = (
                    result.linked.get(decision.method, 0) + 1
                )
                result.links.append((receipt.id, decision.join, decision.method))
                continue
        if mine is None:
            continue
        for other_id, reason in decision.flags:
            other = primary_provenance(conn, other_id)
            if other is not None and insert_candidate(conn, mine, other, reason):
                result.flagged[reason] = result.flagged.get(reason, 0) + 1
                result.flags.append((receipt.id, other_id, reason))
    return result
