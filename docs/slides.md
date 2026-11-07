# whyagent: presentation content

Raw material for the HackTrent slides: an organized dump, not a finished deck. It assumes the whole pipeline works as designed (stages 0–8). Numbers marked **\*** are design targets or expected demo results; replace them with numbers from the final dry runs before presenting. Unmarked numbers come from real runs on the recorded sessions.

---

## 1. Title

- **whyagent**: `git blame` for agent decisions
- Tagline: *"`git blame` tells you which line changed. We tell you which decision caused it, and prove it."*
- HackTrent 2026 · Crystal Ge & Anton
- Main track: **Best Use of Entire**. Also: Gemini, Vultr, Tiger Data, GoDaddy

---

## 2. The problem

**One-liner:** your AI coding agent broke a test. Why did it do that?

- Coding agents now write a large share of commits. They make dozens of edits per session, each one a *decision* backed by reasoning and a reaction to an error they just saw.
- When a test breaks, today's tools only answer "which line changed":
  - `git blame` points at a **line**, but the line that throws is often **not the edit that caused it**. The real cause can be a config change in another file.
  - `git bisect` works on **commits**, but one agent commit can bundle 15+ edits.
  - The *why* (the agent's reasoning and the error it was reacting to) is buried in a long chat transcript that someone has to read by hand.
- So devs scroll through agent logs, guess, and write a rule in `CLAUDE.md` / `AGENTS.md` that **nobody ever checks actually works**.
- And if a lesson isn't validated, it's just a superstition: agent memory fills up with rules that may or may not change anything.

**Pain in three bullets (slide-friendly):**
1. Blame points at the wrong place.
2. The reasoning is lost in a transcript.
3. Lessons we write for agents are never tested.

---

## 3. The solution

**whyagent finds the exact agent edit that broke your test, proves it with real test runs, shows why the agent did it, and only saves a lesson once an experiment shows it works.**

Three promises:
1. **Find the decision**: drill from the commit down to the single agent edit inside the session.
2. **Prove it**: causality comes from experiments: the test passes right before the edit, fails right after, and passes again when only that edit is reverted. No LLM opinion in the verdict.
3. **Learn from it, verified**: generate a lesson, rerun the agent with and without it, and save it to memory only if it's statistically better (Fisher exact test).

Input: one failing test and one known-good commit. Everything else is automatic; even the line to blame is read from the traceback.

```
whyagent --good f39623e --test "pytest tests/test_client.py::test_retries_server_errors_then_succeeds"
```

---

## 3b. Why not just `entire why`? (great for the Entire judges)

Entire already ships `entire why <file:line>`: line → commit → session → prompt. It's `git blame` for agent sessions, and it's a good starting point. whyagent builds on the same data and goes further. Real output on scenario 2:

| | `entire why` | whyagent |
| --- | --- | --- |
| On `client.py:39` (where the traceback points) | Shows the session and prompt behind that line: it points you at the **decoy** | Reverts that edit and reruns the test: **NOT the cause** |
| On `config.py:9` (the real cause) | *"the agent and prompt shown are a best-effort guess"* (the edit was made with `sed`, not an edit tool) | Pins it to the exact `sed -i` command, the agent's reasoning and the prompt, and proves it with test runs |
| Granularity | Commit / session | The single edit inside the session |
| Evidence | Attribution | Causality: passes before, fails after, passes when reverted |
| Afterwards | n/a | Lesson + counterfactual reruns, saved only if validated |

One-liner: *"`entire why` tells you who wrote the line. whyagent tells you which decision broke the test, and proves it."*

---

## 4. How it works (the pipeline)

A simple diagram: **Entire session → edits → experiments → proven root cause → lesson → counterfactual reruns → Brain**.

| Stage | Name | What happens |
| --- | --- | --- |
| 0 | Precheck | HEAD fails, good commit passes. Detects **stable vs flaky** and saves the failure signature |
| 1 | Guilty commit | `git blame` hint on the failing line, checked directly; else `git bisect`. Reads the **Entire checkpoint trailer** on that commit |
| 2 | Rebuild the session | Loads the agent's Entire session and turns it into ordered, replayable edits (incl. edits made with `sed` in a shell). A replay-fidelity check makes sure the edits rebuild the commit byte for byte |
| 3 | Edit-level blame hint | Reverts the edit that wrote the failing line. Passes → confirmed in 2 runs. Still fails → **"NOT the cause"** |
| 4 | Graph pruning | Drops edits the test can't reach (import graph), **verified with one run**, never trusted blindly |
| 5 | Prefix bisect | Binary search over the agent's edits, in order, in parallel across sandboxes |
| 6 | Confirm | Revert that one edit from the final code → test passes. Multiple causes → delta debugging (ddmin) |
| 7 | Reasoning + lesson | Shows the agent's reasoning and the trigger (the error it saw). **Gemini** writes a one-sentence lesson; Brain is checked first for an existing one |
| 8 | Counterfactual reruns | The agent redoes the task from just before the bad edit, N times with the lesson and N times without, in sandboxes. **Fisher exact test**; save to Brain only if it's validated |

**Rigor details (good for a "why trust it" slide):**
- **Three outcomes, not two:** every run is `PASS`, `TARGET_FAIL` (same failure signature as HEAD), or `OTHER` (crash, timeout, different error). `OTHER` never counts as evidence, so a half-finished state with a syntax error can't pose as the bug.
- **Tests are pinned to HEAD** in every state, so an agent that "fixes" a test by editing the test can't hide the bug. (All three real recordings did exactly this!)
- **Flaky mode, sized by statistics:** the precheck measures the failure rate (15 runs), then picks runs per state so a buggy state shows the bug with 95% probability (a 28% bug → 10 runs). Any target failure counts, and a state stops at its first failure. With a fixed 5 runs, a 28% bug hides 19% of the time per state; we saw it pick the wrong edit once before fixing this.
- **Every run is in a throwaway Docker container**, with network off for tests. Agent reruns can only reach the model API.
- **Result cache:** no code state is tested twice.
- **Rerun success means:** the target test passes, everything that passed before still passes (no deleting the feature), and test files are untouched (no tampering).

---

## 5. Demo scenarios (real recorded agent sessions)

All on a seeded **payments API client** repo. The bugs are **real mistakes real agents made** in recorded Entire sessions, not planted by us.

| Scenario | Agent | What the agent did | whyagent result |
| --- | --- | --- | --- |
| **1. Obvious** | Claude Opus | Added pagination, but quietly changed `list_invoices()` to return only page 1 (a breaking API change), then **rewrote the test to match** | Blame hint confirmed in **5 test runs**. Without the hint: commit bisect, 4 of 7 edits pruned (verified), same edit found |
| **2. Sneaky** (blame is wrong) | Claude Haiku | Asked to make a smoke script fail fast; it set `MAX_RETRIES = 1` **globally in `config.py`** with a `sed -i` shell command, and also rewrote an error line in `client.py`. The traceback points at the error line | Blame hint: **NOT the cause** (revert, still fails). Bisect finds `config.py:9`, even though the edit was made with `sed`, not an edit tool. **6 test runs** |
| **3. Flaky** | Claude Haiku | Added a 1-second cache to `get_invoice` with no invalidation. The settlement test now fails ~28% of the time, and the agent **loosened the test deadline** to hide it | Flaky mode: bisected to the cache edit and confirmed, 19–44 runs; right in 4 of 4 runs |

Disclosure for scenario 2: the user steered the agent toward editing `config.py`, and the transcript records it. whyagent still had to find the right edit while blame pointed elsewhere.

**The quote to put on a slide** (the agent's own reasoning right before the bad edit, scenario 2):
> "Lowering `MAX_RETRIES` globally will break the existing retry test, which expects three attempts, so that test needs to set its own retry count."

The agent *knew* the change would break a test and planned to edit the test. That's exactly what whyagent surfaces and what pinned tests defeat.

**Lesson + rerun (expected demo result, run on scenario 1)\*:**
```
lesson: "Never change a public function's return contract to finish a feature; add a new function instead."
rerun: without lesson 0/5 pass · with lesson 5/5 pass · p = 0.004 → VALIDATED, saved to Brain
```

---

## 6. Sample output (terminal, real run on scenario 2)

```
precheck: HEAD fails 5/5, good commit passes 5/5 → stable
commit: blamed 943dc4b ✓ confirmed (parent passes, commit fails)
session: 5fc48f02 · 5 edits · replay matches commit ✓ with 2 edit(s) made by the agent's shell commands
blame hint: edit #1 touches payments/client.py:39 → NOT the cause (revert, still fails)
pruning: 2 edit(s) unreachable from the test → still fails without them ✓ pruned
bisect: 3 edits → 2 test runs → first failing prefix at edit #8
confirm: revert #8 from the final state → test passes ✓ root cause
root cause: edit #8 payments/config.py:9  TIMEOUT_S = 2 / MAX_RETRIES = 1
command: sed -i 's/^TIMEOUT_S = 10$/TIMEOUT_S = 2/; s/^MAX_RETRIES = 3$/MAX_RETRIES = 1/' payments/config.py
reasoning: "Lowering MAX_RETRIES globally will break the existing retry test …"
test runs used: 6
```
(Real output, lightly trimmed. Stage 7–8 lines for the demo come from scenario 1.) The tool also writes an **HTML report**: a session timeline, bisect probes, the guilty diff, reasoning, trigger, the pruning savings chart and rerun results.

---

## 7. Results and why they matter

| Metric | Target | Result |
| --- | --- | --- |
| Correct root cause on recorded bugs | 3/3 | **3/3** |
| Test runs to find the edit | ≤ 6 for 15 edits | **5–6** (stable); flaky: ~10 runs per state, sized from the failure rate |
| Edits pruned before bisecting | measured | 4 of 7 (scenario 1), 2 of 5 (scenario 2), each verified with 1 run |
| Fewer runs with pruning | measured | Measured in Tiger Data: 9 → 8 edit-level test runs across 2 bugs (−11%). Small on these short sessions; grows with session length\* |
| Lessons statistically validated | ≥ 1 | 1\* (p = 0.004\*) |
| End-to-end time per bug | < 5 min | \* |

**Why it matters:**
- **Proof, not vibes.** Every claim is backed by real test runs a human can re-run. That's essential as AI writes more of our code and we need audit trails.
- **Blame can be wrong, and we show it.** Scenario 2 is the case where a human reading the traceback would fix the wrong file.
- **Agent memory that earns its place.** Brain only stores lessons that measurably change agent behaviour, which stops `AGENTS.md` bloat.
- **Caught real agent behaviour:** in all three recordings the agent edited a test to make its change pass. Pinned tests caught it every time.
- **Cheap:** binary search means a handful of test runs (5–6 for a stable bug), no LLM calls to find the cause, and agent tokens are only spent on reruns.
- **Works on real agent behaviour, not just tidy edits:** it traces edits made through shell commands (`sed -i`, `git checkout --`) back to the agent's decision.

---

## 8. Tech stack

- **Python 3.10+** CLI (`whyagent`), ~2.8k lines, 66 tests
- **Entire**: session transcripts plus checkpoint trailers (input data), the session → commit link, Brain (lesson memory)
- **Git**: blame, bisect, worktrees (states are built in throwaway worktrees, never on your branches)
- **Docker**: one image per analysis, a throwaway container per test run, network off; worker pool for parallel runs
- **Gemini** (`google-genai`): writes lessons; **Gemini CLI** is the default rerun agent (Claude Code headless also supported)
- **Vultr** Cloud Compute: VM hosting the sandbox pool so bisect rounds and rerun arms run in parallel
- **Tiger Data** (TimescaleDB / Postgres, `psycopg`): hypertable of every test run and rerun, a continuous aggregate for the pruning-savings chart, and the **shared team Brain** (validated lessons, so every teammate's whyagent sees them)
- **Stats**: one-sided Fisher exact test (no SciPy dependency)
- **Report**: self-contained HTML report + JSON
- **GoDaddy**: project domain
- Cross-platform: developed on macOS and Windows

---

## 9. Sponsor tracks

**Pitch line for Entire:** *"We use Sessions as evidence, Graph to cut test runs, and Brain as a memory that only accepts lessons that are proven to work."*

| Track | What it does in whyagent | Why it isn't bolted on |
| --- | --- | --- |
| **Entire** (main) | Sessions + checkpoint trailers are the core input: commit → session → edits with reasoning and triggers. Graph-style pruning cuts test runs. Brain stores only validated lessons. **Trails:** `--trail` posts the proven root cause as a finding on the agent's trail, on the guilty line, with the revert attached as a one-click suggested patch | Without Entire's commit-to-session link there's no edit-level blame at all |
| **Gemini** | Writes the lesson from the trigger, reasoning, diff and failure; Gemini CLI reruns the task in both arms | The lesson is the hypothesis that the reruns test |
| **Vultr** | Hosts the Docker worker pool; parallel bisect rounds (≈ log base (W+1) of n) and parallel rerun arms | Turns ~10 sequential agent reruns into a couple of minutes |
| **Tiger Data** | Logs every run (stage, state hash, outcome, duration, tokens) in a hypertable; continuous aggregate → runs/tokens per stage, pruned vs blind; stores the team's validated lessons (shared Brain) | It's the evidence for the token-savings claim, and the team's shared memory |
| **GoDaddy** | Domain for the project | n/a |

---

## 10. Use cases

- **Debugging agent-written PRs:** a reviewer sees CI fail on an agent PR and gets the guilty edit plus the agent's reasoning in minutes.
- **CI integration:** run whyagent automatically when a test breaks on an agent commit and post the root cause as a PR comment.
- **Team agent memory:** validated lessons go into a shared memory (Brain) that every agent session reads, so the team's agents stop repeating known mistakes.
- **Prompt and `AGENTS.md` hygiene:** A/B test rules before adding them, and prune ones that don't change behaviour.
- **Agent evaluation:** compare models or agent configs on the same counterfactual task ("does Haiku need this lesson? does Opus?").
- **Auditing and compliance:** prove which automated decision introduced a regression, with reproducible evidence.
- **Catching test tampering:** flag sessions where the agent edited tests to make its change pass.

---

## 11. Future improvements

- **More agents:** adapters for Codex, Cursor, Copilot CLI, Gemini CLI transcripts (anything Entire records).
- **More languages and test runners:** Jest, Go test, cargo test; the generic adapter already works on exit code + output signature.
- **Native Entire Graph and Brain APIs** in place of our static import graph and our lesson store.
- **Automatic trail findings:** run on every agent trail whose CI fails, and post the finding without a human starting it.
- **GitHub App / CI action** that comments the root cause on the failing PR.
- **Auto-regenerate lessons** that test as ineffective, and retest them.
- **Mid-session memory replay:** resume reruns from the agent's exact context instead of a summary.
- **Non-test failure signals:** type checkers, linters, security scanners, perf regressions.
- **Web dashboard** of root causes and lesson win rates over time.

---

## 12. Honest limits (optional slide or Q&A prep)

- Reruns start from a summary of earlier edits, not the agent's exact memory.
- The rerun agent may differ from the original (e.g. Gemini CLI rerunning a Claude session). Both arms use the same agent, so the comparison is fair, and the report says which agent ran.
- Earlier states run with HEAD's dependencies; the report flags sessions that touched dependency files.
- Pruning uses a static import graph; it's always verified with a run, so a miss costs one run, never a wrong answer.

---

## 13. Closing

*"`git blame` tells you which line. whyagent tells you which decision, and proves it."*
