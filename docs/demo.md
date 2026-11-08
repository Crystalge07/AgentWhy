# Seeded demo repo and recording guide

The demo repo is `payments-api-client` (a sibling of this repo): a small Payments API client with a
pytest suite and a fake API, with Entire enabled from the first commit. It contains **no bugs**.
The bugs come from real agent sessions recorded on top of it.

Before recording, check that the repo can still produce all three bugs:

```sh
.venv/bin/python demo/check_seeded.py ../payments-api-client     # Windows: .venv\Scripts\python
.venv/bin/python demo/preflight.py                         # everything else; run before every demo
```

`check_seeded.py` checks a clean snapshot of `main` (`--ref f39623e` for scenario 2b's base), whatever
branch is checked out. `preflight.py` marks Gemini, Claude and Vultr checks SKIP while we lack access.

It plants a hand-written version of each mistake in a throwaway copy and checks the failure
(last verified 2026-11-07: baseline 5/5 pass, scenario 1 and 2 fail 3/3, scenario 3 fails 14/30).
Those patches live here, never in `payments-api-client`, so recorded agents can't see them.

## Try it yourself (for judges)

The judge-facing demo repo is **payments-client**:

- Code: https://github.com/Crystalge07/payments-client (its README has the results table and the reports)
- Sessions: the Entire repo `et/agentwhy/payments-client`, which holds the transcripts
- PRs agentwhy opened (closed ones included): https://github.com/Crystalge07/payments-client/pulls?q=is%3Apr

The tag `seed` is the clean starting point: the Payments client with Entire enabled, before any
README. Branch from it, not from `main`. `main`'s README describes the mistakes, and an agent
working on `main` would read it.

| Branch | Agent | What happened | agentwhy result |
| --- | --- | --- | --- |
| `scenario-1` | Claude Opus 5.5, not steered | Asked to paginate `list_invoices`. It made `list_invoices()` return one page, moved the "all" behaviour to a new `list_all_invoices()`, and rewrote the contract test. It made every edit with one `python3` heredoc, not the Edit tool. The next commit restores the test. | Root cause: the `list_invoices` edit, in 2 runs. Reruns: 0/6 without the lesson vs 6/6 with it, p = 0.001, VALIDATED. `--pr` opened PR #1 using a passing rerun's fix, re-verified at HEAD. |
| `scenario-2` | Claude Haiku 5.5, **steered** | The 2b prompt, then "don't add constructor args, change the defaults in config.py, keep the error message change, commit". The next commit restores the retry test. | `--line payments/client.py:37` (the rewritten raise) is **NOT the cause**. Root cause: the `config.py` defaults, in 6 runs. |

Neither model's reasoning is in the transcript (both hide their thinking, and neither wrote text
before editing), so the reports show the prompt and the exact command or edit instead.

You need Python 3.10+, Docker, Claude Code, the Entire CLI and agentwhy (`pip install -e .` in this repo).

**Rerun an analysis.** This needs read access to the Entire repo, because the session loads from there:

```sh
entire repo clone /et/agentwhy/payments-client && cd payments-client
T="python -m pytest -q -p no:cacheprovider"
git checkout scenario-1 && agentwhy --good seed --no-rerun --test "$T tests/test_client.py::test_list_invoices_returns_every_invoice"
git checkout scenario-2 && agentwhy --good seed --line payments/client.py:37 --no-rerun --test "$T tests/test_client.py::test_retries_server_errors_then_succeeds"
```

**Record your own session** (scenario 1; a GitHub clone works here, since you record the session):

1. Start from the seed and turn on recording:
   ```sh
   git clone https://github.com/Crystalge07/payments-client && cd payments-client
   git checkout -b try seed
   entire enable --agent claude-code     # then start a NEW Claude Code session in this folder
   ```
2. Give Claude Code this prompt, unchanged:
   > Add pagination to `list_invoices`: use the API's `page`/`per_page` params with `config.PAGE_SIZE`,
   > and keep `export_invoices_csv` exporting everything. Commit when done.
3. **What usually happens:** the agent paginates, but changes `list_invoices()` to return only the
   first page and rewrites `test_list_invoices_returns_every_invoice` to match. Agents aren't
   deterministic. If yours gets it right, there's no bug to find, so use the recorded `scenario-1` instead.
4. Put the original test back:
   ```sh
   git checkout seed -- tests/test_client.py && git commit -m "Restore the list_invoices test"
   ```
5. Run agentwhy:
   ```sh
   agentwhy --good seed --test "python -m pytest -q -p no:cacheprovider tests/test_client.py::test_list_invoices_returns_every_invoice" --open
   ```
   **Expect:** the precheck passes at `seed` and fails at HEAD. agentwhy finds the guilty commit
   through its Entire checkpoint and pins the bug to the `list_invoices` edit in a few test runs.
   Stages 7–8 write a lesson and rerun the agent with and without it, which needs Gemini or Claude
   credentials and takes a while. Add `--no-rerun` to stop after the root cause, or `--pr` (on a
   fork you can push to) to get a PR when the lesson is validated.

The older recordings in `payments-api-client` (below) are kept for reference. `payments-client` replaces them
for the demo.

## Recording a scenario

1. In `payments-api-client`, make sure `entire status` says Enabled and that you're on a fresh branch from `main`.
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

All three recordings are real Entire sessions on branches of `payments-api-client`. In each, the agent
**edited an existing test so its change would pass**. A follow-up human commit restores the original test, which is exactly the
case agentwhy pins tests to HEAD for. The demo should say so plainly.

| Branch | Agent | What it did wrong | Restoring commit | agentwhy result |
| --- | --- | --- | --- | --- |
| `scenario-1` | Claude Opus 5.5 | Paginated correctly, but changed `list_invoices()` to return only page 1 (a breaking change) and rewrote `test_list_invoices_returns_every_invoice` to match | `ea7ecb9` restores the test | blame hint on `payments/client.py:43` confirmed in 5 runs; without `--line`: commit bisect, 4 edits pruned (verified), same edit confirmed |
| `scenario-2b-clean` | Claude Haiku 5.5, **steered** | First added constructor args (the right fix). The user then said "don't add constructor args, change the defaults in config.py, keep the error message change, commit", so it set `MAX_RETRIES = 1` / `TIMEOUT_S = 2` globally with `sed -i` (its reasoning notes this will break the retry test) and weakened two retry tests | `e885934` restores the two retry tests | blame hint on `payments/client.py:39` → **NOT the cause**; 2 edits pruned (verified); bisect + confirm on the `config.py` shell edit; 6 runs. Analyze with `--good f39623e` |
| `scenario-3-haiku` | Claude Haiku 5.5 | Added the 1 s cache, saw the settlement test go flaky, and relaxed the test's deadline from 1.5 s to 3 s | `320b6a9` restores 1.5 s | flaky mode (fails ~28–60% depending on machine load): bisected to the `get_invoice` cache edit and confirmed it, 19–44 runs (runs per state sized from the measured failure rate) |

`scenario-3` (Opus) saw the flakiness and fixed it properly, so it has no bug. `scenario-2b` is an
earlier 2b recording polluted by stashed sessions from the failed TLS attempts; kept as a backup only.

**`scenario-1-long` (2026-11-07, not demo-clean).** A longer headless Opus 5.5 session from the seed
with the scenario 1 lure plus side work (docstrings, `--page-size` flag). It made the same real
mistake (`list_invoices()` returns one page) and rewrote the contract test; `b29f46e` restores it.
agentwhy finds the right edit (#24 of 25, 11 pruned) in 7 runs, with and without pruning. But Entire
attached four older sessions with uncommitted work to its checkpoint (primary: `7a566aa1`, a 2b
attempt), so 9 edits are unattributed and the agent's reasoning isn't shown. Local branch only, not
pushed. A clean long recording needs a machine/clone without stale sessions (try `entire clean`
first, after asking).

**`entire why` vs agentwhy (demo beat for the Entire judges).** In `payments-api-client` on
`scenario-2b-clean`, `entire why payments/client.py:39` shows the session behind the decoy line, and
`entire why payments/config.py:9` says its agent/prompt are "a best-effort guess" (the edit was a
`sed -i`). Run both before agentwhy: agentwhy says the first is NOT the cause and pins the second to
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
cd payments-api-client
T="python -m pytest -q -p no:cacheprovider"
git checkout scenario-1        && agentwhy --good 87dc38e --line payments/client.py:43 --test "$T tests/test_client.py::test_list_invoices_returns_every_invoice"
git checkout scenario-2b-clean && agentwhy --good f39623e --line payments/client.py:39 --test "$T tests/test_client.py::test_retries_server_errors_then_succeeds"
git checkout scenario-3-haiku  && agentwhy --good 87dc38e --test "$T tests/test_settlement.py::test_invoice_paid_after_charge_settles"
```

Stages 7–8 alone, from a saved root cause, with a hand-written lesson (no Gemini needed):

```sh
agentwhy --root-cause .whyagent/root_cause.json --test "$T tests/test_client.py::test_list_invoices_returns_every_invoice" \
  --lesson "..." --rerun-agent claude-code --runs 5
```

`--runs 5` is the minimum worth showing: with 2 or 3 runs per arm the Fisher test can't reach p < 0.05,
so nothing can be VALIDATED. Ten agent runs can take up to ~25 min on 2 workers, so **record Stage 8
before the demo** and show it with `agentwhy --render .whyagent/report.json`. To rehearse the plumbing
with no model, use the stub agent (arm B's prompt contains "Lesson"):

```sh
WHYAGENT_STUB_CMD='case "$WHYAGENT_PROMPT" in *Lesson*) true ;; *) <repeat the bad edit> ;; esac' \
  agentwhy --root-cause .whyagent/root_cause.json --test "..." --lesson "..." --rerun-agent stub
```

## Live loop (`--live`)

Runs on the mock model (no Gemini, no network). The mock covers scenario 2b only. Run it from
`payments-api-client` on `scenario-2b-clean`, against the saved root cause:

```sh
WHYAGENT_MODEL=mock agentwhy --live --yes \
  --root-cause .whyagent/s2b/root_cause.json \
  --test "python -m pytest -q -p no:cacheprovider tests/test_client.py::test_retries_server_errors_then_succeeds" \
  --instructions "$(mktemp -d)/AGENTS.md" \
  --report .whyagent/live/report.html
```

**Rehearsals: use a throwaway instructions file.** `--instructions` takes any path, and the rerun agent
still sees the rules as `AGENTS.md` (added after the repo's own `AGENTS.md`, if it has one). The real
`AGENTS.md` stays clean, and each run starts with an empty file, so the rule is written every time
instead of hitting the duplicate check ("already in …, not added again"). A fixed path such as
`--instructions demo/AGENTS.demo.md` works too: delete the file before each run (`rm -f demo/AGENTS.demo.md`).
Drop `--yes` to show the approval prompt live.

Pass `--report` so the live run doesn't overwrite `.whyagent/report.html` from the full analysis.
The HTML report doesn't show the live loop yet; it's in `report.json` under `live`.

What's canned and what's real, so we can say it on stage:
- **Canned:** the rule text and what the mock agent writes. Without the rule it repeats the recorded
  `config.py` edit, except on run 2, where it adds constructor parameters (the good fix). With the rule in
  `AGENTS.md` it always makes the good fix.
- **Real:** the rewind (replayed session in a fresh worktree), every test run in the Docker sandbox, the
  pass/fail evidence and diffs, the `AGENTS.md` write and duplicate check, and the before/after.
- The 2b session was steered ("don't add constructor arguments, change the defaults in config.py"), and the
  rerun prompt is that same instruction. A real agent would get a rule that contradicts its task; disclose this.

## Pipeline view (for judges)

`agentwhy --serve` also serves **`/live`**: an n8n-style workflow canvas that draws one analysis as it runs, in
plain words (no hashes, commands or ids). Each stage is a node that lights up, works, and gets a check mark;
whenever a stage runs tests, a branch of sandbox nodes (one per `--workers`) fans out under it and each shows
what it's testing and the result. The panel below the canvas explains the current stage: the agent's edits as
cards, the blame hint's "not the cause" moment, the search narrowing, the lesson, the reruns.

```sh
cd payments-api-client && agentwhy --serve     # terminal 1: http://127.0.0.1:8700/live
git checkout scenario-2b-clean                   # terminal 2: start the run; /live jumps to it
agentwhy --good f39623e --line payments/client.py:39 --workers 4   --test "python -m pytest -q -p no:cacheprovider tests/test_client.py::test_retries_server_errors_then_succeeds"
```

- `/live/<id>` follows a run as it happens (Server-Sent Events from its `events.jsonl`).
- `/live/<id>?replay=1` plays a finished run back from its saved events (gaps capped at 3 s, a beat after each
  result; Play/Continue, Pause and Restart, Space and R keys; it waits for Play, or add `&play` to start at once). Fallback for bad Wi-Fi, a down VM or no model quota.
  If you show a replay to judges, say it's a recorded run.
- On the Vultr VM the server listens on 127.0.0.1 only: run `agentwhy --serve` there and tunnel it with
  `ssh -L 8700:127.0.0.1:8700 root@<vm-ip>`, then open http://127.0.0.1:8700/live on the laptop.
- Events come from `events.stage()` / `events.emit()` calls in the pipeline (no log scraping); they're a
  no-op unless an analysis turned them on, so library use and tests are unaffected.

## Tiger Data and Vultr pages (for the sponsor tracks)

`agentwhy --serve` also serves two pages, both queried live from Tiger Data (`whyagent/dashboards.py`):

- **`/tiger`**: what we store and how. Totals (analyses, test runs, agent reruns, runs reused from the cache,
  hours of testing, validated lessons), test runs per 30 minutes by result (`time_bucket` on the `runs`
  hypertable), runs per pipeline step, the validated lessons with their without/with evidence and p-values,
  the schema (hypertable, continuous aggregate with a refresh policy, JSONB lessons), and every query's SQL
  and run time. Unit-test rows (labels starting `repo:`) are left out.
- **`/vultr`**: the same bug analyzed on a laptop and on the Vultr VM (time to the proven cause, test runs,
  seconds per test run), and how it's deployed (commit → auto-sync hook → VM → parallel Docker sandboxes →
  Tiger). Each analysis records its host in `analyses.host` (the VM is detected from its DMI vendor;
  `WHYAGENT_HOST` overrides), with `cpus`, `workers` and `finished`.

To add a comparison point, run the same analysis on both machines with the same `--workers` and
`--no-rerun` (test runs only, no model tokens). Deterministic bugs (scenario 1, 2b) compare cleanly; the
flaky one (scenario 3) can take different paths per run, so compare its seconds per test run instead.
