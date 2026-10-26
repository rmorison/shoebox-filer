# ADR-0009: Google Drive Evidence Store With Originals and Content Hashes

## Status

Accepted. Amends [ADR-0003](0003-local-file-store-with-abstraction.md).

## Context

ADR-0003 put the receipt PDFs on the local filesystem behind a `FileStore` interface, and expected S3 or GCS as the next backend. Since then:

- The main consumer, an agent that builds claim and reconciliation packets, needs the **original** evidence (the email with its headers, the original scan), not only the rendered PDF. The pipeline discards originals after rendering.
- Files need to be **verifiable**, and matchable against an external archive without a second copy. Nothing records what a stored file should contain.
- The `FileStore` protocol returns a local `Path`, so no remote backend fits it, and the CLI hardcodes `LocalFileStore`.
- Drive is already a source, with OAuth wired in, and is browsable by the operator and a bookkeeper. S3 or GCS would add an account, a bill and new credentials for under a gigabyte a year.
- Ingest needs to move processed dropoff files out of the dropoff folder. Those files were uploaded by the operator, not created by the app, so `drive.file` can't reach them.

## Decision

1. **Google Drive is the primary evidence store**, behind an `EvidenceStore` interface that works in bytes and opaque references (`drive:<fileId>`, `local:<path>`): `put`, `open` (streamed), `exists`, `stat`, and `rename` for renditions only. `LocalEvidenceStore` stays for devtest, tests and offline use. The backend is chosen in config.
2. **Originals are kept, write-once.** That means the raw RFC 822 message and each attachment for email, and the source file for Drive (adopted into the store, plus a PDF export snapshot for native Google Docs). The rendered PDF stays as a convenience copy. Write-once is enforced by the interface (no update or delete for originals), by Drive's `contentRestrictions.readOnly`, and by verification. It is not tamper-proof against the account owner.
3. **Every stored file has a sha256** in the database (`receipt.stored_files`). Content is addressed by hash, so the same bytes are stored once. Uploads are verified against Drive's `sha256Checksum`, and a `verify` command checks the store, reading metadata by default and re-downloading with `--deep`.
4. **A dedicated Google account holds the full `drive` scope.** It owns the evidence root and shares it with the operator as Viewer. The operator shares only the dropoff folders with it, as Editor. One client wrapper refuses every Drive operation outside the store's own rules: writes only inside the evidence root, moves only from a dropoff into `originals/` or `needs-attention/`, never a permanent delete or a permission change. Every Drive source and the store use this one token, and the operator's `drive.readonly` token is retired.
5. **Layout:** `renditions/YYYY/MM/{date}__{vendor}__{amount}.pdf` (today's naming, renamed when re-extraction changes the metadata, since a Drive file ID survives a rename) and `originals/YYYY/MM/{received-date}__{source-name}__{sha256[:12]}/` (never renamed).
6. **No second copy of the files in this decision.** A replica or a cold archive is a future feature. The database backups stay, with a monthly dump added.

## Consequences

### Positive

- Claims and audits get the original evidence, with a hash that proves it is unchanged.
- Consumers get a stable reference that survives renames and doesn't depend on any one machine's disk.
- The token can't reach the operator's own Drive beyond the shared dropoff folders.
- A future second backend adds rows to `file_locations`, not a new schema.

### Negative

- **One copy of each file stored after cutover** until the second-copy feature lands. Drive trash (30 days), read-only originals and daily verification reduce the risk without removing it.
- A second Google account to keep: its recovery details, two-step verification, and the OAuth app's publishing status (a Testing app's refresh tokens expire after seven days).
- The full `drive` scope is broad, and its limits live in code, not in Google's permission model, beyond what is shared with the account. A leaked token could permanently delete the files the store account owns, and with no second copy that loss would be final. The runbook covers revoking and re-issuing the token.
- Whether the store account can move an operator-owned dropoff file into its own folder is unconfirmed. The Drive backend ticket checks it first, and the design gives the copy fallback.

### Alternatives Considered

- **Local disk primary, Drive as a mirror:** no stable remote reference, and the server's disk stays the only home of originals. Not chosen.
- **S3 or GCS** (ADR-0003's expectation): a new account, bill and credentials, and not browsable without tooling. Not chosen.
- **`drive.file` on the operator's account:** it can't read, list or move operator-uploaded dropoff files. Not chosen.
- **Full `drive` on the operator's account:** it works, but the token reaches the operator's whole Drive. Not chosen.
- **A service account:** no storage quota on a personal Google account, so it can't own the files, and RD-09 rejected it. Not chosen.

## Follow-ups

The implementation tickets are in the [evidence store design](../designs/evidence-store.md#implementation-plan).
