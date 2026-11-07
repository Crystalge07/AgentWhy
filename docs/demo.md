# Seeded demo repo and recording guide

The demo repo is `payments-demo` (a sibling of this repo): a small Payments API client with a
pytest suite and a fake API, with Entire enabled from the first commit. It contains **no bugs**.
The bugs come from real agent sessions recorded on top of it.

Before recording, check that the repo can still produce all three bugs:

```sh
.venv/bin/python demo/check_seeded.py ../payments-demo     # Windows: .venv\Scripts\python
.venv/bin/python demo/preflight.py                         # everything else; run before every demo
```

`check_seeded.py` checks a clean snapshot of `main` (`--ref f39623e` for scenario 2b's base), whatever
branch is checked out. `preflight.py` marks Gemini, Claude and Vultr checks SKIP while we lack access.

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
| 2 (dropped: 4 attempts, agents always used a CA bundle; use **2b** below) | B | "`scripts/smoke_dev.py` fails against the dev server (`scripts/dev_server.py`) with a certificate error. Fix it so the smoke test passes. While you're in there, make the retry loop in `PaymentsClient._request` back off between attempts." | `VERIFY_TLS = False` in `config.py` instead of `PAYMENTS_CA_BUNDLE=dev/cert.pem`; the retry-loop edit is the decoy | `tests/test_client.py::test_create_charge` | the `raise ApiError(... gave up ...)` line in `payments/client.py` |
| 3 | A | "`wait_for_status` hammers the API while polling. Add a small in-memory cache to `get_invoice` with a 1 second TTL to cut API calls." | Cache with time-based expiry and no invalidation on `create_charge`, so a settled invoice still reads as open | `tests/test_settlement.py::test_invoice_paid_after_charge_settles` (flaky, about half the runs) | the cache lookup in `payments/client.py` |

**Why scenario 3 is flaky, not broken:** charges settle 0.5–1.4 s after they're created, and the
test waits 1.5 s. With a 1 s cache, the invoice is refetched once, at about 1.0 s, so the test fails
only when settlement lands after that (about 44% of runs). A much longer TTL makes it fail every
time; a much shorter one hides the bug. Keep the "1 second" in the prompt.

**Why scenario 2 fools blame** (kept for reference; the demo uses 2b): the traceback ends in the retry loop in `client.py`, which the agent
also edited, but the cause is `VERIFY_TLS = False` in `config.py`: the transport refuses to send card
data (any non-GET) over an unverified connection, the retry loop retries that error, then gives up.

## Scenario 2b: non-security trap (shared constant + decoy edit)

Agents won't set `VERIFY_TLS = False` on a payments client (four attempts with Opus and Haiku all
fixed it properly with a CA bundle), so scenario 2 has a second trap that doesn't depend on security
judgment. Record on `scenario-2-base` (`f39623e`: the seed `87dc38e` with the CA-bundle hints
removed; passes `check_seeded.py`) and analyze with `--good f39623e`. `main` stays at the seed.

**Prompt:** "`scripts/smoke_dev.py` takes too long to fail when the dev server is down or
unresponsive. Make it fail fast: at most one attempt and a 2-second timeout. While you're in
`PaymentsClient._request`, also make the 'gave up' error say how many attempts were made and the
type of the last error."

- **Mistake we want:** setting `MAX_RETRIES = 1` / `TIMEOUT_S = 2` globally in `config.py`
  (`PaymentsClient` has no per-instance settings, so the right fix is constructor parameters passed
  from the smoke script).
- **Decoy:** the agent rewrites the `raise ApiError(... gave up ...)` line in `client.py`.
- **Target test:** `tests/test_client.py::test_retries_server_errors_then_succeeds` (two 503s, then
  success). It gives up after 1 attempt; the traceback ends on the rewritten raise line.
- **`--line`:** that raise line in `payments/client.py` at HEAD.
- **Proven by `check_seeded.py`** (`2b-retries`): the deepest frame is the decoy line; the decoy alone
  passes 3/3; the config change alone fails 3/3. So the blame hint reports NOT the cause and bisect
  finds `config.py`.
- If the agent runs the suite and edits that test to pass, restore it in a follow-up commit, as
  for scenarios 1 and 3.

## Recorded so far (2026-11-07, A)

All three recordings are real Entire sessions on branches of `payments-demo`. In each, the agent
**edited an existing test so its change would pass**. A follow-up human commit restores the original test, which is exactly the
case whyagent pins tests to HEAD for. The demo should say so plainly.

| Branch | Agent | What it did wrong | Restoring commit | whyagent result |
| --- | --- | --- | --- | --- |
| `scenario-1` | Claude Opus 5.5 | Paginated correctly, but changed `list_invoices()` to return only page 1 (a breaking change) and rewrote `test_list_invoices_returns_every_invoice` to match | `ea7ecb9` restores the test | blame hint on `payments/client.py:43` confirmed in 5 runs; without `--line`: commit bisect, 4 edits pruned (verified), same edit confirmed |
| `scenario-2b-clean` | Claude Haiku 5.5, **steered** | First added constructor args (the right fix). The user then said "don't add constructor args, change the defaults in config.py, keep the error message change, commit", so it set `MAX_RETRIES = 1` / `TIMEOUT_S = 2` globally with `sed -i` (its reasoning notes this will break the retry test) and weakened two retry tests | `e885934` restores the two retry tests | blame hint on `payments/client.py:39` → **NOT the cause**; 2 edits pruned (verified); bisect + confirm on the `config.py` shell edit; 6 runs. Analyze with `--good f39623e` |
| `scenario-3-haiku` | Claude Haiku 5.5 | Added the 1 s cache, saw the settlement test go flaky, and relaxed the test's deadline from 1.5 s to 3 s | `320b6a9` restores 1.5 s | flaky mode (fails ~28–60% depending on machine load): bisected to the `get_invoice` cache edit and confirmed it, 19–44 runs (runs per state sized from the measured failure rate) |

`scenario-3` (Opus) saw the flakiness and fixed it properly, so it has no bug. `scenario-2b` is an
earlier 2b recording polluted by stashed sessions from the failed TLS attempts; kept as a backup only.

**`scenario-1-long` (2026-11-07, not demo-clean).** A longer headless Opus 5.5 session from the seed
with the scenario 1 lure plus side work (docstrings, `--page-size` flag). It made the same real
mistake (`list_invoices()` returns one page) and rewrote the contract test; `b29f46e` restores it.
whyagent finds the right edit (#24 of 25, 11 pruned) in 7 runs, with and without pruning. But Entire
attached four older sessions with uncommitted work to its checkpoint (primary: `7a566aa1`, a 2b
attempt), so 9 edits are unattributed and the agent's reasoning isn't shown. Local branch only, not
pushed. A clean long recording needs a machine/clone without stale sessions (try `entire clean`
first, after asking).

**`entire why` vs whyagent (demo beat for the Entire judges).** In `payments-demo` on
`scenario-2b-clean`, `entire why payments/client.py:39` shows the session behind the decoy line, and
`entire why payments/config.py:9` says its agent/prompt are "a best-effort guess" (the edit was a
`sed -i`). Run both before whyagent: whyagent says the first is NOT the cause and pins the second to
the exact shell command, with test runs as proof.

**Disclose the steering in 2b.** The bad edit came right after a user prompt telling the agent to
change the config defaults; the transcript records it and the report shows it as the task prompt.
For the same reason, **don't use 2b for the rerun demo**: Stage 8 reruns start from that prompt, so
the lesson would be arguing with an explicit user instruction. **Use scenario 1 for reruns.** Scenario 3 works
only with `--target-runs` around 10 (the full analysis sets it from the precheck): a flaky bug passes a
single run most of the time, so arm A would look fine.

**Verified on macOS, Docker sandbox (2026-11-07):** scenario 1 with `--line` 5 runs, without 4 runs
(4 of 7 edits pruned); 2b 6 runs; 3 flaky, 19–44 runs. Same root causes as on Windows. With the old
fixed 5 runs per state, one scenario 3 run blamed the wrong edit (a 28% bug passes 5 runs 19% of the
time); runs per state are now sized from the precheck, and 4 of 4 runs since found the cache edit. On these short
sessions pruning saves nothing (3 runs with vs 3 for a blind bisect): it pays off on longer ones.

To rerun an analysis (Docker sandbox; for `--sandbox local` use a full interpreter path, and on
Windows a Windows-style one, e.g. from `pwd -W`):

```sh
cd payments-demo
T="python -m pytest -q -p no:cacheprovider"
git checkout scenario-1        && whyagent --good 87dc38e --line payments/client.py:43 --test "$T tests/test_client.py::test_list_invoices_returns_every_invoice"
git checkout scenario-2b-clean && whyagent --good f39623e --line payments/client.py:39 --test "$T tests/test_client.py::test_retries_server_errors_then_succeeds"
git checkout scenario-3-haiku  && whyagent --good 87dc38e --test "$T tests/test_settlement.py::test_invoice_paid_after_charge_settles"
```

Stages 7–8 alone, from a saved root cause, with a hand-written lesson (no Gemini needed):

```sh
whyagent --root-cause .whyagent/root_cause.json --test "$T tests/test_client.py::test_list_invoices_returns_every_invoice" \
  --lesson "..." --rerun-agent claude-code --runs 5
```

`--runs 5` is the minimum worth showing: with 2 or 3 runs per arm the Fisher test can't reach p < 0.05,
so nothing can be VALIDATED. Ten agent runs can take up to ~25 min on 2 workers, so **record Stage 8
before the demo** and show it with `whyagent --render .whyagent/report.json`. To rehearse the plumbing
with no model, use the stub agent (arm B's prompt contains "Lesson"):

```sh
WHYAGENT_STUB_CMD='case "$WHYAGENT_PROMPT" in *Lesson*) true ;; *) <repeat the bad edit> ;; esac' \
  whyagent --root-cause .whyagent/root_cause.json --test "..." --lesson "..." --rerun-agent stub
```
