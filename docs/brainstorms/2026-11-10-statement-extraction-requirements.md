---
date: 2026-11-10
topic: statement-extraction
---

# Statement Extraction and the Ledger's Read Contract

## Problem Frame

The operator's books are kept in a separate ledger project. It imports bank and card lines as facts, classifies them, and matches them to receipts. Structured bank files (CSV, OFX, QFX) go straight to the ledger. Many periods, mostly the older backlog, exist only as PDF statements. Someone has to turn those into lines, and the boundary between the two projects is settled: **shoebox extracts, the ledger classifies.**

This filer already keeps evidence documents, reads PDFs under bounds, extracts with Haiku and serves a read-only MCP server. So statements come here. The filer reads each PDF statement in a separate watched folder, extracts its accounts and lines, checks them against the statement's own printed figures, keeps the original, and serves the result to the ledger through a published, versioned read contract. A statement that doesn't check out is held. Nothing from it is passed on as a clean extraction.

A feasibility spike ran outside this repository, on real statements that never came here. It settled the method: the text layer sent to Haiku, a first pass that classifies the document and lists its accounts, then one extraction call per account. Deterministic parsing isn't viable. A single all-accounts call dropped accounts. Sonnet did no better than Haiku. The operator signed off the go and the design decisions below on 2026-11-09.

The same change renames the roadmap's "Reconciliation" lane to document extraction, since reconciliation now belongs to the ledger.

---

## Key Flows

- F1. Statement intake
  - **Trigger:** The operator drops a statement PDF into the statement folder.
  - **Steps:** `serve` sees the new file → stores the original in the evidence store → reads the text layer under statement bounds → first pass: document type and account list → rejects a non-statement, notice or no-activity bill without extracting → one extraction call per listed account → per-account gates → records the statement, its accounts and the lines of clean accounts.
  - **Outcome:** The statement is `rejected`; `held`, with a reason (the account list failed, or no account is clean or, for a loan, figures-only); `incomplete` (at least one account held by its own checks, at least one clean or figures-only); or `complete` (every account clean or figures-only). Exactly one applies. Each is visible in the CLI and over MCP.
  - **Covered by:** R1–R14

- F2. The ledger imports
  - **Trigger:** The ledger polls the change feed with its last cursor.
  - **Steps:** List statements new or changed since the cursor → fetch each one → re-check the arithmetic itself → import clean accounts' lines as facts → raise a review item for each held account.
  - **Outcome:** The ledger holds the lines of every clean account, and knows which accounts are held, why, by how much and on which pages.
  - **Covered by:** R15–R22

- F3. A held account
  - **Trigger:** The operator reviews a held account (the ledger's review item, or `receipt-index statements list --status held`).
  - **Steps:** Read the failure class, difference and page range → either fix it at the source (a structured file to the ledger, or lines entered there by hand) or re-run the extraction once a prompt or model change is in place.
  - **Outcome:** A re-run makes a new revision that must pass the same gates. Nothing overrides a gate.
  - **Covered by:** R11, R12, R18

---

## Requirements

**Intake**
- R1. Statements arrive through their own watched source: a Drive folder configured as a statement source. A statement never enters the receipt pipeline, and a receipt source never feeds the statement pipeline.
- R2. Intake runs inside `serve`, with the existing sync state, item locks, `started` rows and retry rules.
- R3. The original PDF is kept in the evidence store as a write-once, hashed original. The same bytes arriving twice are one statement.
- R4. A PDF with no usable text layer (a scan) is rejected as out of scope for this version, with that reason. It is not sent to the model.

**Bounds**
- R5. Statements get their own PDF bounds (pages, CPU, wall clock, memory, text size), separate from receipts', and the receipt bounds don't change. A statement over a bound is rejected with the bound named.

**Document-type gate**
- R6. A first pass reads the whole text layer and returns the document type (deposit, card, loan statement, loan bill, tax or escrow notice, not a statement), the accounts it covers with their page ranges, and any account count the document prints.
- R7. Only deposit, card and loan statements are extracted. The others are rejected with their type and no extraction call. Knowing a document's kind counts as extraction.

**Extraction**
- R8. Each listed account is extracted by its own call, from the whole text layer, with a prompt for its account type, told which account and pages. Every call goes to the configured extraction model (Haiku), with no deterministic parsing.
- R9. A deposit or card account yields its institution, last four digits only, currency, period (its own where printed, else the document's), stated beginning and ending balances, printed totals, and its lines: posted date (the line's one date when it prints only one), transaction date where a second is printed, description as printed, amount, running balance where printed, section heading and page. Dates printed without a year take the year that places them in the period.
- R10. A loan account yields only figures printed on the statement (principal before and after, principal paid, interest, escrow, fees, advances), each null when not printed. Shoebox never computes one, and doesn't check them: the ledger does.

**Gates**
- R11. A deposit or card account is clean only when all of these hold:
  - its beginning balance plus its lines equals its ending balance, to the cent;
  - each line's posted date is inside the statement period, or for a card, up to a few days before it;
  - each printed running balance follows from the one before it;
  - each printed section or column total matches its lines;
  - it has lines, or it is dormant (no lines, and equal printed beginning and ending balances).
- R12. An account that fails a gate is held with a failure class, the difference where there is one, and its page range. An account with no printed beginning balance is held, and so is one too long to extract in one call. There is no override.
- R13. A statement is complete only when every listed account is clean (or, for a loan, figures-only) and, where the document prints an account count, the counts agree. A held account never contributes a line. A clean account on the same statement does, unless the account list itself fails (an empty account that isn't dormant, a loan with no printed figure, a count that disagrees, or no accounts at all): then every account is held, the statement says why, and each account keeps its own failure class. A held statement always carries its reason.
- R14. Every extraction records its model, prompt versions and token use, and keeps every earlier revision.

**The read contract**
- R15. The ledger reads statements over the MCP server, never the tables. The contract is published in this repository and versioned. Every response names its version.
- R16. Per statement: a stable ID, the original's SHA-256, document type, period, status and the reason for a hold, page count, revision and whether it is current, extractor model and prompt versions.
- R17. Per account: institution, last four digits, account type, currency, period, stated balances and totals, status (clean, held or figures only), and for a held account its failure class, difference and page range.
- R18. Per line, clean accounts only: posted date, transaction date where printed, description, amount, running balance where printed, section and page. Amounts are signed by their effect on the statement's own balance, the sign the gate verified. The ledger converts to its own books' sign.
- R19. Everything needed to re-check the arithmetic of a clean account is in the responses: balances, every line, section and column totals, the account's period and currency, and exact formats for money, dates and timestamps. A held account carries no lines, by design.
- R20. A change feed lists statements new or changed since an opaque cursor, without skipping a statement whose transaction commits late, and with a way to put every statement back through the feed after a database restore. The receipts change feed follows the same cursor convention.
- R21. The original PDF can be fetched over MCP, with each fetch access-logged, as `get_receipt_file` is.
- R22. The CLI shows the same data, with `--output json` in the contract's shapes.

**Lane rename**
- R23. The roadmap's "Reconciliation" lane becomes document extraction, without classification. Reconciliation and classification belong to the ledger.

---

## Key Decisions

Settled by the operator on 2026-11-09, not reopened here:

- Deposit and card statements, single-account and combined. Scans are out of the first version.
- A separate watched folder for statements.
- The document-type gate first: classify and list accounts, and reject non-statements, notices and no-activity bills without extracting.
- Text layer to Haiku, one call per listed account with an account-type prompt, after the first pass. No deterministic parsing, and no single all-accounts call.
- Gates per account: to the cent; dates in the period with a few days' grace before it on card lines; running balances consistent where printed. Complete only when every listed account is clean.
- Held, never partial: an account that doesn't reconcile is held, and so is a statement with no printed beginning balance. No override.
- Loan statements: printed figures only, never a computed one. The ledger checks them.
- Shoebox extracts and never classifies.

Decided here:

- **Amount sign: the statement's own balance.** A line is positive when it raises the printed balance and negative when it lowers it. That is the sign the gate verifies, so the ledger re-foots the same figures with no transformation. Turning it into the ledger's debit-positive sign is one flip by account type (a card's balance is owed). Choosing the owner's side of the books is an accounting reading, the ledger's side of the boundary. Shoebox emits account type and section, which are extraction.
- **Section and column totals are a gate too.** The spike's grader used them. A printed total that disagrees with the lines is held as a totals mismatch. The operator confirmed it on 2026-11-10.
- **Clean accounts of an incomplete statement pass their lines.** The hold is per account. This follows the spike's recommendation that a clean account imports while another on the same document is held.
- **No automatic retry of a held account in this version.** A re-run is by hand and produces a new revision.
- **Statements live in the `receipt` schema**, beside the evidence-store tables they reference, not in a new schema.

---

## Scope Boundaries

- No scans or image statements (no vision path).
- No CSV, OFX or QFX import: the ledger takes those directly.
- No classification, matching or categorisation of lines, and no link from a line to a receipt.
- No computed loan figure, and no loan roll-forward check.
- No override, partial import of a held account, or hand-entered line in shoebox.
- No IMAP statement source in this version. A statement that arrives by email still goes through the receipt pipeline as today, where it reads as a receipt of kind `statement`.
- No chaining of a missing beginning balance from the previous statement.
- No change to the receipts read contract beyond agreeing the cursor convention. The receipts change feed is its own ticket.

---

## Success Criteria

- Every synthetic fixture layout below is extracted clean, or held with the expected failure class, in tests with a stubbed model. Each gate is seen to fail first.
- No non-statement, notice or no-activity bill produces a line.
- No account that fails a gate produces a line, and no account on a statement whose account list fails (a dropped account or a count that disagrees) produces a line.
- The ledger's session pre-reviews the contract and signs it off before the build.
- In production, after a week of real statements, held accounts carry enough to act on (class, difference, pages) without opening the database.

---

## Dependencies / Assumptions

- The evidence store's local backend keeps the originals. The Drive backend isn't needed.
- The operator's statement folder is on the Drive account the existing Drive source already reads.
- Haiku 5.5 at medium effort stays the configured model. A model change is its own decision.
- The ledger re-checks the arithmetic at import rather than trusting shoebox's status.

---

## Outstanding Questions

### Answered by the operator (2026-11-10)
- [R11] A printed section or column total that disagrees with the lines holds the account.
- [R11] Card grace: 7 days before the period start.
- [R13] A clean account on an incomplete statement passes its lines, and a failed account list holds every account.
- [F3] No automatic retry of a held statement in this version.

### Deferred to planning
- [Affects R5][Technical] Statement bound values.
- [Affects R20][Technical] Cursor mechanism that can't skip a late commit.
- [Affects R3][Technical] Folder layout for statement originals in the store.

---

## Next Steps

→ `docs/plans/2026-11-10-001-feat-statement-extraction-plan.md` plans the build.
