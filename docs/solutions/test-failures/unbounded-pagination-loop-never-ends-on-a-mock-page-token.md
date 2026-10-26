---
title: An unbounded pagination loop never ends on a MagicMock page token
date: 2026-10-26
category: test-failures
module: adapters/gdrive
problem_type: test_failure
component: service_layer
symptoms:
  - "An integration test run stops printing after a few dozen tests and never finishes"
  - The pytest process keeps growing in memory (gigabytes) while it holds the shared integration-test lock
  - Other sessions queued on the same lock wait indefinitely
root_cause: missing_validation
resolution_type: code_fix
severity: high
related_components:
  - testing_framework
  - development_workflow
tags: [pagination, page-token, magicmock, infinite-loop, google-drive, flock, test-timeout]
---

# An unbounded pagination loop never ends on a MagicMock page token

## Problem

The Drive adapter paged through an API response with an uncapped loop that stopped only when `nextPageToken` was `None`. Older integration tests stubbed the Drive service with a bare `unittest.mock.MagicMock`, which answers `response.get("nextPageToken")` with another truthy `MagicMock`, never `None`. The loop never ended. The run held the shared devtest integration lock for about half an hour and blocked another session queued behind it. Memory grew to several gigabytes as the mock recorded every call the loop made on it.

## Symptoms

- `make test-integration` (or a direct `pytest tests/integration`) prints progress dots, then stops. The process doesn't exit or fail.
- The test process's memory keeps climbing. Nothing looks wrong on the network: no open IMAP or HTTP traffic to explain the wait.
- Anyone else waiting on the integration lock (`flock` on the shared lock file) sits behind it with no output.

## What Didn't Work

- **Suspecting a network or IMAP IDLE wait.** The new code had just added IMAP and Drive I/O, so a blocked socket was the obvious guess. The process's open sockets showed no live IMAP or Drive connection.
- **Waiting it out, or giving it a longer run.** An infinite loop doesn't finish. A run with no time limit just holds the lock until someone kills it.

What found it was the test collection order. Comparing the count of completed tests in the log with `pytest --collect-only -q` named the hung test: the first test in the mocked-Drive class that ran a second ingest. A second ingest is the first one to take the changes-feed path rather than the folder-listing path.

## Solution

There are three parts, and each one stops this failure on its own.

1. **Bound every pagination loop, and reject a page token that can't be real.** Both Drive loops, the changes feed and the folder listing, now run a fixed number of pages at most. They raise if the next token isn't a string or repeats the current one (`src/receipt_index/adapters/gdrive.py:243-268` for the changes feed, `:362-385` for the folder listing):

   ```python
   # before
   while True:
       response = service.changes().list(pageToken=page_token, ...).execute()
       ...
       next_page = response.get("nextPageToken")
       if next_page is None:
           new_token = str(response["newStartPageToken"])
           break
       page_token = next_page

   # after
   for _page in range(_MAX_CHANGE_PAGES):
       response = service.changes().list(pageToken=page_token, ...).execute()
       ...
       next_page = response.get("nextPageToken")
       if next_page is None:
           new_token = str(response["newStartPageToken"])
           break
       if not isinstance(next_page, str) or next_page == page_token:
           raise RuntimeError(f"Drive changes feed returned a bad page token: {next_page!r}")
       page_token = next_page
   else:
       raise RuntimeError(f"Drive changes feed longer than {_MAX_CHANGE_PAGES} pages")
   ```

2. **Give mocks real page tokens.** The mocked-Drive integration tests now answer the account, start-token and changes-feed calls with real dicts. That includes a final page with `newStartPageToken` and no `nextPageToken` (`tests/integration/test_phase2.py:793`, `_wire_changes_feed`). Unit tests for the adapter use a small fake service that returns dicts, not `MagicMock` chains.

3. **Time-limit any test run that holds a shared lock.** Put the timeout *inside* the lock, so a hang ends the run and releases the lock:

   ```bash
   flock <shared-lock-file> timeout -k 10 900 make test-integration
   ```

   Two tests in `tests/unit/test_gdrive_adapter.py` pin the guards: `test_a_repeating_page_token_fails_instead_of_looping` and `test_a_repeating_listing_token_fails_instead_of_looping`. Each feeds a page token that repeats and asserts the call raises instead of looping.

## Why This Works

The real API ends paging by leaving `nextPageToken` out, so the loop's only exit was "the key is absent". A `MagicMock` has no absent keys: every attribute and every `.get()` returns a fresh, truthy mock, so that exit could never be reached. A page cap makes termination independent of what the server (or a stub) returns. The type and repeat check turns the two plausible malformed answers into an immediate, named error rather than thousands of pointless pages. Neither guard changes behaviour against the real API.

This was latent in production too. A server that returned the same token twice would have looped the same way while holding the source's advisory lock. The mock only made it certain.

## Prevention

- **Every `while True` around a paging API gets a page cap and a token check.** The shape is `for _ in range(MAX): ... else: raise`. Before following a token, check `isinstance(token, str)` and that it differs from the current one. Pick a cap far above any real result size, so hitting it means a bug, not a big account.
- **Don't stub a paging client with a bare `MagicMock`.** Give the stub an explicit last page: a dict with no next-page key. Better, use a small fake that returns dicts. If a `MagicMock` must stay, set `execute.return_value` to a concrete dict for every paging call the code under test can reach, including paths only a *second* run takes.
- **Test the guard itself.** Add a unit test that feeds a repeating token and asserts the call raises. Then break the guard and watch that test fail.
- **Put a timeout inside every shared lock.** `flock <lock> timeout 900 <command>` releases the lock when the command hangs. `timeout <command>` outside the lock, or no timeout at all, does not. Give tests that wait on a network their own short timeout too, such as a socket timeout on the IMAP client.

## Related Issues

- `src/receipt_index/adapters/gdrive.py`: the changes-feed and folder-listing loops.
- `tests/integration/test_phase2.py`: `_wire_changes_feed`, the real-token wiring for the mocked-Drive tests.
