---
title: Tests that mock the agent hid two breaks in the locked LLM stack
date: 2026-10-27
category: test-failures
module: extraction
problem_type: integration_issue
component: service_layer
symptoms:
  - "Every ingested or re-extracted item is recorded as failed with zero LLM tokens"
  - "TypeError: Cannot instantiate typing.Union from pydantic_ai/models/anthropic.py, raised before any request is sent"
  - "Once that was fixed: TypeError: 'RunUsage' object is not callable, raised after the model replied"
  - Unit and integration tests all pass, and CI is green
root_cause: wrong_api
resolution_type: dependency_update
severity: critical
related_components:
  - testing_framework
  - development_workflow
tags: [pydantic-ai, anthropic, magicmock, uv-lock, stub-server, keyless-test, dependency-pairing]
---

# Tests that mock the agent hid two breaks in the locked LLM stack

## Problem

Every receipt extraction failed in production, and every test passed. Two separate breaks sat between our code and the Anthropic API, and the test suite could see neither of them.

1. **A library pairing break.** The lock had pydantic-ai 2.59 with anthropic 2.1.0. pydantic-ai 2.59 built each message by calling `BetaMessageParam(role=..., content=...)`. In anthropic 1.x that name is a `TypedDict`; in 2.x it is a `Union` alias, and calling it raises `TypeError: Cannot instantiate typing.Union`. pydantic-ai-slim requires `anthropic>=1.15.0` with no upper bound, so the resolver had accepted the pairing.
2. **A call to a property.** `_to_extraction_result` read token usage with `result.usage()`. On the run result, `usage` is a property that returns a `RunUsage`. Calling it raises `TypeError: 'RunUsage' object is not callable`, and only after the model has replied. The first break hid this one, which surfaced only once the pairing was fixed.

## Why the tests missed both

- **Every test that touched extraction mocked the agent.** A `MagicMock` result has no idea what pydantic-ai or anthropic do, so the real message-building code never ran. Lock bumps could change that code freely while every test stayed green.
- **The mocks matched the bug, not the library.** The usage mocks were written as `result.usage.return_value = usage`. A `MagicMock` attribute is callable, so `result.usage()` returned the stub and the test passed. The mock encoded the same wrong shape as the code, so the two agreed with each other and not with pydantic-ai.
- **The one real call never ran in CI.** The vision integration test called the real API, so it skipped without a key, and CI has no key.

The pagination learning found the same blind spot from another side: [An unbounded pagination loop never ends on a MagicMock page token](unbounded-pagination-loop-never-ends-on-a-mock-page-token.md). There, a `MagicMock` answered with a value the real API never sends. Here, it accepted a call the real object refuses. In both cases the mock was more permissive than the dependency it stood for.

## Solution

1. **Fix the pairing at the floor, not with a cap.** Set `pydantic-ai>=2.60` in `pyproject.toml`. The 2.60.0 release notes read "Support `anthropic` 2.x: build message params as dicts". Then relock only that family with `uv lock -P pydantic-ai -P pydantic-ai-slim`. Pinning `anthropic<2` also worked, but it holds the SDK on an old major that someone would have to lift later.
2. **Read the property as a property**: `usage = result.usage` in `src/receipt_index/extraction.py`. Give every mock the real shape, `result.usage = usage` rather than `result.usage.return_value = usage`, so a mock rejects the call form just as the library does.
3. **Run one keyless call through the locked stack.** `tests/unit/test_extraction_stack.py` starts a stdlib `ThreadingHTTPServer` on 127.0.0.1 that answers `POST /v1/messages` like the Messages API. It points the SDK at that server with `monkeypatch.setenv("ANTHROPIC_BASE_URL", ...)` and a dummy `ANTHROPIC_API_KEY`. Then it calls `extract_metadata(raw, agent=create_extraction_agent(...))` with no mocks:

   - **text**: an email with a text body;
   - **image**: a Drive PNG, which sends an `image` block;
   - **PDF**: a Drive PDF with no text layer, which falls back to a `document` block.

   Each case asserts the parsed `ReceiptMetadata`, the token counts from the stub's usage, and the content block types the stub received.

   Things to know when writing the stub:
   - **pydantic-ai's `run_sync` streams.** The request has `"stream": true`, and a plain JSON reply fails with "Streamed response ended without content or tool calls". The stub has to answer with server-sent events: `message_start`, `content_block_start` for a `tool_use` block, a `content_block_delta` carrying `input_json_delta`, `content_block_stop`, `message_delta` with `stop_reason` and usage, then `message_stop`.
   - **Answer with the tool the request declares.** Structured output arrives as a call to the agent's output tool, so the stub replies with a `tool_use` named `request["tools"][0]["name"]` rather than hard-coding the name.
   - **Leave the provider's `base_url` unset.** pydantic-ai's Anthropic provider passes `base_url=None`, so the SDK reads `ANTHROPIC_BASE_URL` from the environment. The test needs no hook in production code.

## Proof that the check catches both breaks

On the old lock, with the new test file:

```
$ uv run --frozen pytest tests/unit/test_extraction_stack.py -q --no-cov
E       TypeError: Cannot instantiate typing.Union
3 failed
```

With the dependency fixed but `result.usage()` still called as a method:

```
E       TypeError: 'RunUsage' object is not callable
3 failed
```

With both fixes, all three pass. The test runs in the unit job, so CI needs no key and no Docker.

## Prevention

- **Keep one unmocked call through each external SDK the code depends on.** Point it at a local stub server, not at the vendor. A stub at the HTTP layer exercises the locked library code, which is the code a lock bump changes. Mocks above that layer can't see such a change.
- **Make a stub reply valid for the real protocol,** including streaming when the client streams. A reply the client rejects tests nothing.
- **Shape each mock like the real object.** A property is mocked as a value, not as `.return_value`. Where possible, build a real instance or use `create_autospec`, which rejects calls the real class doesn't accept.
- **Watch the new check fail on the broken lock** before trusting it. Swap the old `uv.lock` back in, run the test, and expect the production error.
- **When a library has no upper bound on a dependency,** expect the resolver to pair them in ways upstream never tested. The stub test is the guard; an upper bound only delays the problem.

## Related

- [An unbounded pagination loop never ends on a MagicMock page token](unbounded-pagination-loop-never-ends-on-a-mock-page-token.md): another case of a `MagicMock` being more permissive than the real API.
- `src/receipt_index/extraction.py`: `create_extraction_agent` and `_to_extraction_result`.
- `tests/unit/test_extraction_stack.py`: the stub server and the three keyless cases.
