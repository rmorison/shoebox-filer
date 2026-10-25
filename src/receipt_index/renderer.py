"""Receipt-to-PDF rendering.

Receipt HTML is untrusted third-party content rendered unattended, so both
rendering engines run with network egress blocked: only inline resources
(``data:`` URIs embedded in the document) are served and every remote
reference is refused. This removes the exfiltration path a renderer exploit
would need, and makes PDFs deterministic — no remote images or trackers are
fetched at render time.
"""

from __future__ import annotations

import base64
import html
import logging
import os
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import Route

    from receipt_index.models import Attachment, RawReceipt

logger = logging.getLogger(__name__)

#: URL schemes whose payload travels with the document — fetching them causes
#: no network or filesystem I/O. Everything else (http, https, file, ftp,
#: protocol-relative and relative references) counts as remote.
INLINE_URL_SCHEMES = frozenset({"data", "about", "blob"})


class RemoteResourceBlockedError(Exception):
    """Raised when a render attempts to fetch a non-inline resource."""


PLAIN_TEXT_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: monospace; font-size: 12px; margin: 2em; }}
  .header {{ border-bottom: 1px solid #ccc; padding-bottom: 1em; margin-bottom: 1em; }}
  .header p {{ margin: 0.2em 0; }}
  pre {{ white-space: pre-wrap; word-wrap: break-word; }}
</style>
</head>
<body>
<div class="header">
  <p><strong>Subject:</strong> {subject}</p>
  <p><strong>From:</strong> {sender}</p>
  <p><strong>Date:</strong> {date}</p>
</div>
<pre>{body}</pre>
</body>
</html>
"""


def render_pdf(raw: RawReceipt) -> bytes:
    """Render a RawReceipt to PDF bytes.

    Routes based on source_type:
    - "gdrive": return file directly (PDF) or convert image to PDF
    - Otherwise: email rendering (existing path)
    """
    if raw.source_type == "gdrive":
        return _render_drive_file(raw)
    return _render_email(raw)


def _render_drive_file(raw: RawReceipt) -> bytes:
    """Render a Drive-sourced file to PDF.

    - PDF files: return content directly (already a PDF).
    - Image files: convert to PDF via image_to_pdf.
    """
    if raw.file_content is None:
        raise ValueError("Drive-sourced receipt has no file_content")

    content_type = (raw.file_content_type or "").lower()

    if content_type == "application/pdf":
        return raw.file_content

    if content_type in {"image/jpeg", "image/png"}:
        from receipt_index.image_converter import image_to_pdf

        return image_to_pdf(raw.file_content)

    raise ValueError(
        f"Unsupported file content type for Drive rendering: {content_type!r}"
    )


def _render_email(raw: RawReceipt) -> bytes:
    """Render an email-sourced receipt to PDF.

    Strategy (in order of preference):
    1. If a PDF attachment exists, return it directly
    2. If HTML body exists, render via Playwright (fallback: weasyprint)
    3. If text body exists, wrap in HTML template and render via weasyprint
    4. Render a minimal page with just the email headers via weasyprint
    """
    # 1. PDF attachment pass-through
    pdf_data = _find_pdf_attachment(raw.attachments)
    if pdf_data is not None:
        return pdf_data

    # 2. HTML body rendering (Playwright for complex email CSS)
    if raw.html_body:
        return _render_html_to_pdf(raw.html_body, raw.attachments)

    # 3. Text body rendering (weasyprint is fine for simple templates)
    if raw.text_body:
        return _render_text_to_pdf(raw)

    # 4. Minimal fallback — headers only
    return _render_text_to_pdf(raw)


def _find_pdf_attachment(attachments: list[Attachment]) -> bytes | None:
    """Return the data of the first PDF attachment, or None."""
    for att in attachments:
        if att.content_type.lower() == "application/pdf":
            return att.data
    return None


def _render_html_to_pdf(html_content: str, attachments: list[Attachment]) -> bytes:
    """Render HTML to PDF, embedding any inline images."""
    html_with_images = _embed_inline_images(html_content, attachments)
    return _html_to_pdf_bytes(html_with_images)


def _embed_inline_images(html_content: str, attachments: list[Attachment]) -> str:
    """Replace cid: references with data: URIs from attachments."""
    attachment_map: dict[str, Attachment] = {}
    for att in attachments:
        if att.content_type.startswith("image/"):
            # Map by filename (which may be the Content-ID)
            attachment_map[att.filename] = att

    if not attachment_map:
        return html_content

    def replace_cid(match: re.Match[str]) -> str:
        cid = match.group(1)
        att = attachment_map.get(cid)
        if att is None:
            return match.group(0)
        b64 = base64.b64encode(att.data).decode("ascii")
        return f"data:{att.content_type};base64,{b64}"

    return re.sub(r"cid:([^\s\"'>]+)", replace_cid, html_content)


def _render_text_to_pdf(raw: RawReceipt) -> bytes:
    """Wrap text body in HTML template and render to PDF."""
    body = html.escape(raw.text_body or "(no body content)")
    rendered = PLAIN_TEXT_TEMPLATE.format(
        subject=html.escape(raw.subject),
        sender=html.escape(raw.sender),
        date=html.escape(raw.date.isoformat()),
        body=body,
    )
    return _html_to_pdf_weasyprint(rendered)


def _html_to_pdf_bytes(html_content: str) -> bytes:
    """Convert HTML string to PDF bytes.

    Tries Playwright (browser-quality rendering) first, falls back to
    weasyprint if Chromium is not installed.
    """
    try:
        return _html_to_pdf_playwright(html_content)
    except ImportError:
        logger.debug(
            "Playwright not installed, falling back to weasyprint", exc_info=True
        )
    except Exception:
        logger.warning(
            "Playwright rendering failed, falling back to weasyprint", exc_info=True
        )
    return _html_to_pdf_weasyprint(html_content)


def _is_inline_url(url: str) -> bool:
    """Report whether a URL can be served without leaving the render process.

    Args:
        url: URL a rendering engine is about to fetch.

    Returns:
        True for inline schemes (see :data:`INLINE_URL_SCHEMES`), False for
        every remote or ambiguous reference.
    """
    return urlsplit(url).scheme.lower() in INLINE_URL_SCHEMES


def _block_remote_requests(route: Route) -> None:
    """Playwright route handler that aborts every non-inline request.

    Aborting rather than stalling matters: a blocked resource fails
    immediately, so the page finishes rendering instead of waiting out a
    network timeout.

    Args:
        route: Intercepted Playwright route for a request made by the page.
    """
    url = route.request.url
    if _is_inline_url(url):
        route.continue_()
        return
    logger.debug("Blocked remote request during render: %s", url)
    route.abort()


def _inline_only_url_fetcher(url: str) -> Any:
    """Serve inline URLs for weasyprint and refuse remote ones.

    Args:
        url: URL weasyprint wants to fetch.

    Returns:
        A weasyprint URL fetcher response for inline URLs.

    Raises:
        RemoteResourceBlockedError: If the URL is not inline. weasyprint
            converts fetcher exceptions into a non-fatal ``URLFetchingError``,
            so the render continues with the resource missing.
    """
    if not _is_inline_url(url):
        logger.debug("Blocked remote resource during render: %s", url)
        raise RemoteResourceBlockedError(f"Remote resource blocked: {url}")

    try:
        from weasyprint.urls import URLFetcher
    except ImportError:  # weasyprint < 68
        from weasyprint.urls import default_url_fetcher

        return default_url_fetcher(url)

    return URLFetcher(allowed_protocols=INLINE_URL_SCHEMES).fetch(url)


def _weasyprint_url_fetcher() -> Any:
    """Build the ``url_fetcher`` argument for ``weasyprint.HTML``.

    weasyprint >= 70 requires a ``URLFetcher`` instance (it reads attributes such
    as ``_fail_on_errors`` off the fetcher), so the inline-only function is wrapped
    in a subclass. Older releases accept the function directly.

    Returns:
        A fetcher that delegates every URL to ``_inline_only_url_fetcher``.
    """
    try:
        from weasyprint.urls import URLFetcher
    except ImportError:  # weasyprint < 68
        return _inline_only_url_fetcher

    class _InlineOnlyURLFetcher(URLFetcher):  # type: ignore[misc]
        def fetch(self, url: str, headers: Any = None) -> Any:
            return _inline_only_url_fetcher(url)

    return _InlineOnlyURLFetcher(allowed_protocols=INLINE_URL_SCHEMES)


def _html_to_pdf_playwright(html_content: str) -> bytes:
    """Convert HTML string to PDF bytes via Playwright headless Chromium.

    Every request the document makes is intercepted; non-inline requests are
    aborted before they reach the network.
    """
    from playwright.sync_api import sync_playwright

    # Ensure PLAYWRIGHT_BROWSERS_PATH is set for local installs
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = ".playwright"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            # JS off: email HTML never legitimately needs scripts (mail clients
            # do not run them), and route() cannot intercept WebSockets that
            # script could open — disabling JS closes that egress gap outright.
            context = browser.new_context(java_script_enabled=False)
            context.route("**/*", _block_remote_requests)
            page = context.new_page()
            page.set_content(html_content, wait_until="domcontentloaded")
            pdf_bytes: bytes = page.pdf(format="Letter")
            return pdf_bytes
        finally:
            browser.close()


def _html_to_pdf_weasyprint(html_content: str) -> bytes:
    """Convert HTML string to PDF bytes via weasyprint.

    Uses an inline-only URL fetcher so remote references are refused rather
    than fetched, matching the Playwright path.
    """
    import weasyprint

    doc = weasyprint.HTML(string=html_content, url_fetcher=_weasyprint_url_fetcher())
    return doc.write_pdf()  # type: ignore[no-any-return]
