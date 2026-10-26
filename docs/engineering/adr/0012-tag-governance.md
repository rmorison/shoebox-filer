# ADR-0012: Receipt Annotations: A Controlled Tag Vocabulary and an Append-Only History

## Status

Proposed

## Context

Receipts need tags that map to accounting tracking codes, and links to records in other systems. Both a person (through the CLI) and an agent (through future MCP write tools) will set them. Hand corrections to extracted fields, and links between a refund and its purchase, are changes of the same kind: made after extraction, by someone, for a reason.

Key factors:

- **Tags must map to tracking codes.** Free-form tags drift ("parking", "Parking", "car-parking") and stop mapping cleanly.
- **More than one kind of actor.** A person, an agent, and rules in the application (such as linking a refund at ingest).
- **Accountability.** For claims and reconciliation, it must be possible to say who coded a receipt, when, and why, and that record must not be editable by the actors it records.
- **Scale and trust.** One household runs the tool. There is no user authentication, and adding some only for this would be out of proportion.

## Decision

### A controlled vocabulary

A tag must be defined in `receipt.tags` before it can be applied. A name is lower case, optionally with one namespace (`category:value`), and can carry a tracking code. Tags are deactivated, never deleted. A deactivated tag can't be applied, and stays where it is already applied.

### Who may do what

| Action | Person | Agent |
|---|---|---|
| Define, rename, deactivate a tag | Yes | No |
| Apply or remove a defined, active tag | Yes | Yes |
| Add or remove an external link | Yes | Yes |
| Edit an extracted field, link a refund | Yes | No |

The boundary is enforced by a database role. Migrations grant to a `NOLOGIN` role, `receipt_index_dev_annotate`, with `SELECT` on the receipt tables, `INSERT` and `DELETE` on `receipt_tags` and `external_links`, and nothing else. Agent write tools connect as a login that is a member of that role only and owns nothing. The CLI keeps the `_write` role. Because a migration can't create roles, a one-off superuser step creates the role on existing databases.

### One append-only history

Every change after extraction writes one row to `receipt.receipt_events` in the same transaction as the change: the event type, a JSON payload (such as the old and new value, or the tag's ID and name), the actor, the actor kind (`person`, `agent` or `rule`) and an optional reason. The current state (`receipt_tags`, `external_links`, the receipt's fields) is kept in its own tables for querying; the history is the record of how it got there, and the only place that says who made a change.

- **Tag and link events are written by triggers** on `receipt_tags` and `external_links`, so no caller can skip one or write one that doesn't match. The triggers take the actor from a transaction-local setting, refuse a change with no actor, and record `agent` for any session whose login is a member of the annotate role.
- **History can't be changed by application code.** No application role has `UPDATE` or `DELETE` on it, and since production connects as the role that owns the tables, a guard trigger also refuses every update, delete and truncate, apart from the cascade when a receipt itself is deleted. The owning role could still drop the trigger by a deliberate schema change; whether to separate the owner from the application's login is left to the operator.

The actor's name is attribution, not authentication: the CLI records a configured name, and an agent tool records the MCP client's name.

## Consequences

### Positive

- **Tags stay mappable.** Each one has at most one tracking code, and only a person adds to the list.
- **An agent can code receipts but can't redefine the codes**, edit amounts, rewrite history or record itself as a person, whatever its prompt says.
- **One query answers "what happened to this receipt"**, across edits, tags, links and refund links.
- **History survives mistakes.** A wrong tag is removed with a second event, and both stay on record.

### Negative

- **A person must define a tag before an agent can use it**, which adds a step when a new category comes up.
- **A new database role** to create and grant in devtest and production, by a superuser step outside the migrations.
- **Triggers carry logic** (writing events, guarding history) that would otherwise sit in Python, and need their own tests.
- **Deleting a receipt deletes its history.** The CLI has no delete command, and merging duplicates moves history first.
- **Attribution can be wrong.** Whoever runs the CLI can give any name; this is accepted for a household tool.
- **The history grows without bound.** At household volumes this is small; there is no pruning.

### Alternatives Considered

- **Free-form tags.** No setup step, but drift and no tracking-code mapping. Not chosen.
- **Tags as a text array on the receipt.** Simple, but no vocabulary, no per-tag attribution and no history. Not chosen.
- **One history table per change type.** The same columns several times over, and a union for every question. Not chosen.
- **History kept only in application logs.** Not queryable, and lost with the log rotation. Not chosen.
- **User accounts and authentication.** The right answer for a shared service, out of proportion for one household. Not chosen; revisit if the service is ever offered over HTTP to more than one party.

## Follow-ups

- Ticket 3 of the [receipt data-model design](../designs/receipt-data-model.md): `receipt_events`, with hand edits.
- Ticket 7: the vocabulary, `receipt_tags` and the annotate role.
- Ticket 8: external links.
- MCP write tools for tags and links, as the annotate role.
