# ADR-0005: Playwright for HTML-to-PDF Rendering

## Status

Accepted

## Context

The renderer module converts email HTML bodies to PDF when no PDF attachment exists. The current implementation uses weasyprint, a pure-Python CSS renderer. While lightweight and easy to install, weasyprint has limited CSS support — no flexbox, no grid, no JavaScript execution. Complex email HTML (designed for browser-based email clients) renders poorly: text columns pushed off-page, missing prices and line items.

**Example**: A Netflix Shop order confirmation with 16 line items renders correctly in a browser print-to-PDF but produces a weasyprint PDF with product images only and all text/prices clipped.

Key factors:

- **Rendering fidelity**: Email HTML assumes browser-quality CSS rendering
- **Speed**: This is a batch CLI tool, not a real-time service — 1-3s per page is acceptable
- **Dependency weight**: Chromium is ~200MB, but can be installed locally in the project tree
- **Existing path**: PDF attachment pass-through and text-to-PDF rendering work fine with weasyprint

## Decision

Use **Playwright** (`playwright.sync_api`) with headless Chromium for HTML-to-PDF rendering, with **weasyprint as a fallback** when Chromium is unavailable.

### Strategy

1. Try Playwright `page.pdf()` for HTML-to-PDF conversion (browser-quality output)
2. If Playwright/Chromium is not installed, fall back to weasyprint
3. Keep weasyprint for `_render_text_to_pdf()` (simple HTML template, no complex CSS)

### Browser Management

Chromium is installed locally in the project tree via `PLAYWRIGHT_BROWSERS_PATH=.playwright`, avoiding system-level side effects. The browser version is pinned to the Playwright Python package version. Setup is part of `make setup`.

## Consequences

### Positive

- **Browser-identical rendering**: Handles any CSS layout email clients support
- **No system-level install**: Chromium lives in `.playwright/` within the project tree
- **Graceful degradation**: Falls back to weasyprint if Chromium is absent
- **Version controlled**: Browser version pinned via Playwright package version in `pyproject.toml`

### Negative

- **Disk footprint**: ~200MB for Chromium binary in `.playwright/`
- **Slower rendering**: ~1-3s per page vs ~100ms with weasyprint
- **Process overhead**: Launches a browser process per rendering call (future optimization: reuse browser instance within a pipeline run)

### Alternatives Considered

- **Weasyprint only (status quo)**: Simpler, but produces unusable PDFs for complex email HTML. Not chosen.
- **Puppeteer**: Node.js-based, would require a separate runtime. Not chosen since we're a Python project.
- **wkhtmltopdf**: Deprecated, uses an old WebKit. Not chosen.
