# ADR-0004: PDF Text Extraction via pdfplumber with Claude Vision Fallback

## Status

Accepted

## Context

Receipts arriving as PDF attachments cannot be processed by the current extraction pipeline. The `_build_prompt()` function in `extraction.py` only includes email body text (plain text or HTML-stripped), so when a receipt's amounts, line items, or vendor details exist only inside a PDF attachment, the LLM has insufficient context and extraction fails or produces low-confidence results.

Two categories of PDF receipts exist in practice:

- **Text-based PDFs**: Machine-generated invoices from vendors (Amazon, utilities, SaaS providers). Text is embedded and directly extractable.
- **Scanned/image PDFs**: Photographed or scanned paper receipts forwarded as attachments. No selectable text; requires OCR or vision.

Key factors:

- **Common case**: The majority of PDF attachments from email receipts are text-based (vendor-generated)
- **Cost sensitivity**: Text extraction should avoid unnecessary API calls when possible
- **Existing integration**: The project already uses Anthropic Claude via pydantic-ai
- **System dependency minimization**: Avoid heavyweight system-level dependencies (e.g., Tesseract, Poppler CLI tools)

## Decision

Use **pdfplumber** for text-based PDF extraction as the primary strategy, with **Claude vision via pydantic-ai `BinaryContent`** as a fallback for scanned/image PDFs.

### Strategy

1. Attempt text extraction with pdfplumber (fast, local, no API cost)
2. If extracted text is insufficient (< 20 non-whitespace characters), fall back to Claude vision
3. Pass the extracted text as supplemental context to the metadata extraction prompt

### Architecture

A standalone `pdf_reader.py` module with no email/IMAP dependencies, callable from any adapter context.

## Consequences

### Positive

- **No system-level OCR dependency**: Claude vision handles image PDFs without Tesseract or similar
- **Cost efficient**: Text-based PDFs (the common case) use only local processing — no API call
- **Leverages existing integration**: Vision fallback uses the same Anthropic API key and pydantic-ai patterns already in the project
- **Lightweight**: pdfplumber is a pure-Python library with minimal dependencies
- **General purpose**: The module works for any PDF source, not just email attachments

### Negative

- **Vision fallback cost**: Each scanned PDF triggers an API call (~$0.01-0.03 depending on page count)
- **New runtime dependency**: pdfplumber adds to the dependency tree (though it is lightweight)
- **Threshold tuning**: The "insufficient text" threshold (20 characters) is heuristic and may need adjustment based on real-world data

### Alternatives Considered

- **pdfplumber only (no vision fallback)**: Simpler, but fails silently on scanned PDFs. Not chosen because scanned receipts are a real use case.
- **Tesseract OCR**: Industry-standard OCR but requires a system-level install (`apt install tesseract-ocr`), adds deployment complexity, and produces lower accuracy than Claude vision for receipt data. Not chosen.
- **Claude vision only (no pdfplumber)**: Simpler code path but sends every PDF to the API unnecessarily. Most receipts are text-based and extractable locally. Not chosen due to unnecessary cost and latency.
- **PyMuPDF (fitz)**: More capable than pdfplumber (handles some image extraction), but has AGPL licensing concerns for non-open-source use. Not chosen.

## Follow-ups

- Monitor the 20-character threshold against real receipt data and adjust if needed
- Consider caching vision extraction results if the same PDF is reprocessed
- Track API cost of vision fallback calls in logging
