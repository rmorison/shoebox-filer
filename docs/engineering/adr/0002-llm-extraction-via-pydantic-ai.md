# ADR-0002: LLM-Powered Metadata Extraction via Pydantic-AI

## Status

Accepted

## Context

Receipt emails arrive in diverse formats — forwarded vendor confirmations, HTML order summaries, attached PDF invoices, plain-text receipts. Extracting structured metadata (vendor name, transaction amount, date) from this variety is the core challenge of the ingestion pipeline.

Key factors:

- **Format diversity**: No single regex or heuristic can reliably parse all receipt formats
- **Extraction targets**: Vendor name (string), amount (decimal), date (date) — well-defined, structured output
- **Accuracy requirements**: Must be reliable enough that the reconciler trusts search results without manual verification of extraction quality
- **Cost sensitivity**: Processing ~200 existing + ~10-20 new receipts/week; cost per extraction matters
- **Existing experience**: Anthropic API already used in other projects; team familiar with the tooling

## Decision

Use **Anthropic Claude Haiku** model for metadata extraction, orchestrated via **Pydantic-AI** for structured output and agentic patterns.

## Consequences

### Positive

- **Format agnostic**: LLM handles the diversity of email receipt formats without format-specific parsers
- **Structured output**: Pydantic-AI provides typed, validated output models — extraction results are Pydantic objects, not raw strings
- **Cost effective**: Haiku is the most affordable Claude model; at ~200 receipts, total extraction cost is negligible (sub-dollar)
- **Maintainability**: No brittle regex or template-matching code to maintain per vendor
- **Extensibility**: Extraction prompts can be tuned to add new fields (e.g., tax amount, currency) without code changes
- **Standards contribution**: This project may inform new engineering standards for agentic patterns with Pydantic-AI

### Negative

- **External dependency**: Requires Anthropic API access and a valid API key
- **Latency**: Each extraction is an API call (~1-3 seconds); batch processing ~200 receipts takes several minutes
- **Non-deterministic**: Same input may produce slightly different extractions across runs (mitigated by low temperature and structured output constraints)
- **Cost at scale**: If volume grows significantly, per-call costs accumulate (mitigated by idempotent processing — each receipt extracted once)

### Alternatives Considered

- **Regex/heuristic parsing**: Deterministic and free, but brittle across receipt formats. Would require per-vendor templates and constant maintenance. Not chosen due to format diversity.
- **Open-source LLM (local)**: Eliminates API dependency and cost but requires GPU resources, model management, and typically lower extraction quality. Not chosen for early-stage simplicity.
- **OpenAI GPT**: Comparable capability, but team has existing Anthropic experience and API access. No compelling reason to introduce a second LLM provider.

## Follow-ups

- Define Pydantic models for extraction output in technical design
- Establish extraction prompt strategy (system prompt + email content)
- Consider retry/fallback logic for API failures
- Document extraction accuracy observations to inform potential standards
