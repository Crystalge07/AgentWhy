# Seeded demo repo and recording guide

The demo repo is `payments-demo` (a sibling of this repo): a small Payments API client with a
pytest suite and a fake API, with Entire enabled from the first commit. It contains **no bugs**.
The bugs come from real agent sessions recorded on top of it.

Before recording, check that the repo can still produce all three bugs:

```sh
.venv/bin/python demo/check_seeded.py ../payments-demo     # Windows: .venv\Scripts\python
```

It plants a hand-written version of each mistake in a throwaway copy and checks the failure
(last verified 2026-11-07: baseline 5/5 pass, scenario 1 and 2 fail 3/3, scenario 3 fails 14/30).
Those patches live here, never in `payments-demo`, so recorded agents can't see them.

## Recording a scenario

1. In `payments-demo`, make sure `entire status` says Enabled and that you're on a fresh branch from `main`.
2. Start a **new** Claude Code session (sessions started before `entire enable` aren't recorded).
3. Paste the task prompt below and let the agent work. Don't hint at the mistake. Aim for about 15 edits.
4. Let the agent commit. The commit must get an `Entire-Checkpoint:` trailer.
5. Run the target test. If it still passes, the agent avoided the trap: reset the branch and retry,
   or tweak the wording. If it never falls for it, hand-write the session and say so in the demo.
6. Never paste keys or secrets into a recorded session; transcripts upload on push.

| # | Owner | Task prompt (the lure) | Mistake we want | Target test | `--line` |
| --- | --- | --- | --- | --- | --- |
| 1 | A | "Add pagination to `list_invoices`: use the API's `page`/`per_page` params with `config.PAGE_SIZE`, and keep `export_invoices_csv` exporting everything." | Page count off by one, e.g. `total // size` instead of rounding up, so the last partial page is dropped | `tests/test_export.py::test_export_includes_every_invoice` | the page-count line in `payments/client.py` |
| 2 | B | "`scripts/smoke_dev.py` fails against the dev server (`scripts/dev_server.py`) with a certificate error. Fix it so the smoke test passes. While you're in there, make the retry loop in `PaymentsClient._request` back off between attempts." | `VERIFY_TLS = False` in `config.py` instead of `PAYMENTS_CA_BUNDLE=dev/cert.pem`; the retry-loop edit is the decoy | `tests/test_client.py::test_create_charge` | the `raise ApiError(... gave up ...)` line in `payments/client.py` |
| 3 | A | "`wait_for_status` hammers the API while polling. Add a small in-memory cache to `get_invoice` with a 1 second TTL to cut API calls." | Cache with time-based expiry and no invalidation on `create_charge`, so a settled invoice still reads as open | `tests/test_settlement.py::test_invoice_paid_after_charge_settles` (flaky, about half the runs) | the cache lookup in `payments/client.py` |

**Why scenario 3 is flaky, not broken:** charges settle 0.5–1.4 s after they're created, and the
test waits 1.5 s. With a 1 s cache, the invoice is refetched once, at about 1.0 s, so the test fails
only when settlement lands after that (about 44% of runs). A much longer TTL makes it fail every
time; a much shorter one hides the bug. Keep the "1 second" in the prompt.

**Why scenario 2 fools blame:** the traceback ends in the retry loop in `client.py`, which the agent
also edited, but the cause is `VERIFY_TLS = False` in `config.py`: the transport refuses to send card
data (any non-GET) over an unverified connection, the retry loop retries that error, then gives up.

## Recorded so far (2026-11-07, A)

Both recordings are real Entire sessions on branches of `payments-demo`. In both, the agent
avoided the planted trap but made a different, real mistake: it **edited an existing test so its
change would pass**. A follow-up human commit restores the original test, which is exactly the
case whyagent pins tests to HEAD for. The demo should say so plainly.

| Branch | Agent | What it did wrong | Restoring commit | whyagent result |
| --- | --- | --- | --- | --- |
| `scenario-1` | Claude Opus 5.5 | Paginated correctly, but changed `list_invoices()` to return only page 1 (a breaking change) and rewrote `test_list_invoices_returns_every_invoice` to match | `ea7ecb9` restores the test | blame hint on `payments/client.py:43` confirmed in 5 runs; without `--line`: commit bisect, 4 edits pruned (verified), same edit confirmed |
| `scenario-3-haiku` | Claude Haiku 5.5 | Added the 1 s cache, saw the settlement test go flaky, and relaxed the test's deadline from 1.5 s to 3 s | `320b6a9` restores 1.5 s | flaky mode (fails ~28%): bisected to the `get_invoice` cache edit and confirmed it, 20 runs |

`scenario-3` (Opus) saw the flakiness and fixed it properly, so it has no bug. Scenario 2 (B) is not
recorded yet.

To rerun an analysis (Windows: use a Windows-style interpreter path, e.g. from `pwd -W`):

```sh
cd payments-demo && git checkout scenario-1
whyagent --sandbox local --good 87dc38e --line payments/client.py:43   --test "$PWD/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_client.py::test_list_invoices_returns_every_invoice"
```
