# Long-Term Roadmap (Working Draft)

Started 2026-11-02, after the always-on ingest work reached production and development paused. This draft collects the lanes of longer-term work and the use cases behind them. Nothing in it is ordered or committed: the operator sets priorities closer to execution time, and each piece still goes through the usual spec, plan and sign-off. The live sprint plan is the repository's roadmap issue.

## Lanes

Listed in no particular order.

1. **Reconciliation.** Import card, bank and loan statements, match their lines to receipts and payments, and report what's left unmatched. Use cases are below.
2. **Extraction quality, measured.**
   - An evaluation set of synthetic and anonymized receipts with expected fields, scored for each prompt version together with its cost, so that a prompt change and any back-catalogue re-extraction are decided on numbers.
   - The rest of the receipt data model: line items, full-text search, tags and external links.
   - Pending extraction improvements: invoices versus payments, the issuing agency as the vendor, and an in-store purchase's transaction number as its order number.
3. **The assistant as a working user.**
   - MCP over HTTP with authentication.
   - Write tools for tags, links, corrections and duplicate review decisions.
   - Packet export: size-bounded merged PDFs with a CSV schedule, for claims and tax.
4. **Evidence integrity and storage.**
   - The Drive storage backend on a dedicated account, and moving the local tree to it.
   - The dropoff archive, with processed items moved to done or needs-attention locations.
   - Scheduled verification with alerts.
   - Re-checking originals marked unavailable.
   - Dropping legacy columns.
   - A second copy or a cold archive, with recovery drills rather than only recovery documents.
5. **Sources.** Retailer order-history exports, phone scans as a first-class source, and more mailboxes.
6. **Operations and hardening.**
   - The small open fixes and the Python version move.
   - Bounding image and HTML processing as PDF processing is bounded.
   - Splitting the largest modules.
   - Trend metrics: latency, cost per receipt and failure rate.

**Open questions for the operator:**
- Is reconciliation the north star, or is a searchable, deduplicated archive the main value?
- Will anyone else run this? If so, a packaging and contributor-experience lane belongs here.
- Is the Drive migration still wanted?
- How far may the assistant act without review?

## Use cases

### Loan payment splits and escrow (reconciliation)

*Captured 2026-11-02. No priority assigned.*

**Today, by hand.**
- A mortgage payment leaves the bank as one transaction, but the lender applies it as principal, interest and escrow.
- Escrow is drawn down from time to time to pay property taxes or insurance.
- After the statement period closes, the operator reads the lender's statement, then goes back to the payment transaction in the accounting system (Xero) and corrects its split categories.
- When escrow pays a tax or insurance bill, the operator makes an entry in the mortgage escrow account.
- SBA loans and other accounts need similar manual processes, each with its own split (for example principal, interest and fees).

**What the filer could do.**
- **Ingest** periodic loan and account statements as documents, as it does receipts, from email or the Drive dropoff.
- **Extract** each statement's structured content:
  - the statement period;
  - each payment applied: date, total, and its split into principal, interest, escrow, fees or other components;
  - escrow disbursements: date, payee type (taxes, insurance), amount;
  - running balances: principal remaining, escrow balance.
- **Match** each applied payment to the bank transaction that paid it, on date and amount. This ties in with statement-line matching elsewhere in reconciliation.
- **Produce what the accounting system needs:** the split for each payment transaction by account category, and a journal entry for each escrow disbursement into or out of the escrow account. The form is open: a reviewable report the operator applies by hand, an import file, or later a direct integration.
- **Generalize** across mortgages, SBA loans and other amortizing loans or accounts whose statements carry a per-payment breakdown. The mapping from each component to an account category is configured per account.

**Open questions, for planning time:**
- The accounting-system path: a report, an import file, or an API.
- Per-account configuration of split components and target categories.
- How a correction is reviewed before it's applied.
- Re-matching after late or corrected statements.
- How this relates to receipts already classified as statements.
