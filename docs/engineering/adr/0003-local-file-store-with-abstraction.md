# ADR-0003: Local File Store with Abstraction Layer

## Status

Accepted

## Context

Each ingested receipt produces a PDF rendition that needs to be stored and retrievable via search results. The product spec requires human-readable file naming (`{date}__{vendor}__{amount}.pdf`) and a directory layout compatible with future cloud storage migration (S3/GCS).

Key factors:

- **Current deployment**: Local filesystem, single machine
- **Future migration**: Cloud storage (S3/GCS) is a stated future goal
- **Human readability**: Files should be browsable and understandable in a file manager without the application
- **Volume**: ~200 existing + ~10-20/week; modest and manageable in a flat-ish directory structure
- **Open question (OQ-01)**: Directory structure — year or year/month subdirectories

## Decision

Use a **local filesystem store** behind a **storage abstraction interface**, with a **year/month subdirectory structure**.

### Directory Structure

```
{store_root}/
├── 2025/
│   ├── 01/
│   │   ├── 2025-01-05__amazon__42.99.pdf
│   │   ├── 2025-01-12__costco__157.32.pdf
│   │   └── ...
│   ├── 02/
│   │   └── ...
│   └── ...
└── 2026/
    └── ...
```

### File Naming

`{YYYY-MM-DD}__{vendor_slug}__{amount}.pdf`

- **Date**: ISO format `YYYY-MM-DD`
- **Vendor slug**: Lowercase, alphanumeric + hyphens, max 50 characters (e.g., `amazon`, `home-depot`, `trader-joes`)
- **Amount**: Decimal with period separator, no currency symbol (e.g., `42.99`, `1250.00`)
- **Separator**: Double underscore `__` for unambiguous parsing

### Storage Interface

```python
class FileStore(Protocol):
    def save(self, receipt_date: date, vendor: str, amount: Decimal, pdf_data: bytes) -> str: ...
    def get_path(self, receipt_id: str) -> Path | str: ...
    def exists(self, receipt_id: str) -> bool: ...
```

This resolves **OQ-01** from the product specification.

## Consequences

### Positive

- **Human browsable**: Year/month layout maps naturally to how receipts are reviewed (by period, e.g., monthly reconciliation)
- **Manageable directory sizes**: At ~10-20 receipts/month, each month directory stays small and scannable
- **Cloud-compatible**: Year/month path prefixes translate directly to S3/GCS key prefixes, which also optimizes list operations
- **Abstraction enables migration**: Swapping `LocalFileStore` for `S3FileStore` requires only a new implementation of the same interface
- **Metadata in filename**: Files are self-describing even without the database

### Negative

- **Vendor name collisions**: Slugification may produce identical slugs for different vendors (mitigated by including date and amount, and by appending a short suffix if needed)
- **Long filenames**: Some receipts may produce unwieldy paths (mitigated by slug length cap)
- **Filename-based retrieval is fragile**: Renaming a file breaks the database reference (mitigated by storing the relative path in the database, not reconstructing it)

### Alternatives Considered

- **Year-only subdirectories**: Simpler, but at 120-240 receipts/year the directory becomes harder to browse. Not chosen.
- **Flat directory**: Simplest, but doesn't scale and provides no temporal organization. Not chosen.
- **UUID-based naming**: Guaranteed unique but not human-readable. Contradicts the product requirement for browsable files. Not chosen.
- **Database-only storage (BLOBs)**: Eliminates filesystem management but prevents human browsing and makes backups harder. Not chosen.

## Follow-ups

- Implement `LocalFileStore` class with the defined interface
- Define vendor name slugification rules
- Handle duplicate filename edge cases (append numeric suffix)
- Store relative path (from store root) in database for portability
