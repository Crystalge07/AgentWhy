# whyagent — PRD

Nov 7, 2026 · @crystal

## Overview

`git blame` tells you which line changed. agentwhy tells you which agent decision caused a failing test, proves it by experiment, and proves the fix works.

**Problem.** When a coding agent breaks something, devs dig through chat logs by hand to work out why it did that. Blame points at a line, but the line that breaks is often not the edit that caused it, and blame says nothing about the reasoning behind it.

**Approach.** Blame is the fast first guess. Bisect is the engine that confirms it or finds the real cause. Counterfactual reruns then test whether a written lesson actually stops the agent repeating the mistake.

**Built on** Entire, which records agent sessions (prompts, reasoning, tool calls) and links each one to its commit through a checkpoint trailer. Entire supplies the raw data; agentwhy does the causal analysis, which is about 90% Python.

## Goals, non-goals, success metrics

The v1 bar: on every seeded bug, agentwhy names the correct root-cause edit and backs it with test runs, not an LLM's opinion.

**Goals**

- Work on any repo with a test command, judging pass or fail by exit code alone.
- Find the guilty commit, then the guilty edit inside its agent session.
- Confirm causality by reverting the edit and rerunning the test.
- Show the agent's reasoning and the trigger (the error it was reacting to) at the guilty step.
- Validate a lesson with counterfactual reruns before saving it to Brain.

**Non-goals for v1**

- The Intercept security scanner. Tests are the only failure signal.
- Perfect replay of the agent's mind. Reruns are an approximation, and we say so.
- Agent sessions edited heavily by hand. Human edits get flagged, not analyzed.

**Success metrics**

| Metric | Target |
| --- | --- |
| Correct root cause on seeded bugs | 3 of 3 |
| Test runs to locate the edit, 15-edit session | 6 or fewer |
| Bugs with a statistically validated lesson | At least 1 |
| End-to-end time per bug, reruns included | Under 5 min |
| Fewer test runs with Graph pruning vs blind bisect | Measured and shown |

## User flow and CLI

The user points agentwhy at one failing test and a known-good commit; everything else is automatic.

```
agentwhy \
  --test "pytest tests/test_auth.py::test_login" \
  --good a1b2c3d \
  --line src/auth.py:42 \
  --runs 5 \
  --setup "pip install -r requirements.txt"
```

| Flag | Required | Meaning |
| --- | --- | --- |
| `--test` | Yes | Command that runs one failing test. Exit 0 is pass, anything else is fail. |
| `--good` | Yes | A commit where the test passes. Stage 0 verifies it. |
| `--line` | No | The line the failure points at, used as the blame hint. Parsed from a pytest traceback if omitted. |
| `--runs` | No | Reruns per arm in the counterfactual test. Default 5. |
| `--setup` | No | Environment setup command, or a Dockerfile in the repo root. |
| `--no-rerun` | No | Stop after root cause, skipping counterfactual reruns to save tokens. |

**Expected output**

```
precheck: HEAD fails 3/3, good commit passes 3/3 → stable
commit: blamed 9f3e21 ✓ confirmed (parent passes, commit fails)
session: 2026-11-08-abc123 · 15 edits · replay matches commit ✓
blame hint: edit #7 touches src/auth.py:42 → NOT the cause (revert, still fails)
bisect: 15 edits → 4 test runs → first failing prefix at edit #3
confirm: revert #3 from HEAD → test passes ✓ root cause
root cause: edit #3 config.py:12  verify=False
trigger: SSLError: certificate verify failed
reasoning: "cert error, disabling verification to unblock"
lesson: "Never disable SSL verification; fix or mount the certificate."
rerun: without lesson 0/5 pass · with lesson 5/5 pass · p = 0.004 → VALIDATED, saved to Brain
```

## Core logic pipeline

An edit counts as the root cause only if the test passes right before it, fails right after it, and passes again when that edit is reverted from the final code. Every stage below exists to establish one of those three facts with real test runs.

### Ground rules

- **Three outcomes, not two.** Every test run is PASS (exit 0), TARGET\_FAIL (non-zero exit with the same failure signature as HEAD), or OTHER (crash, timeout, import error, or a different failure). OTHER is treated like `git bisect skip`: it never counts as evidence. Without this, a half-finished intermediate state with a syntax error would look like the bug.
- **Failure signature.** The pytest adapter uses test ID + exception type + normalized message. The generic adapter hashes the last 20 lines of output with numbers, paths, hex and timestamps stripped.
- **Tests are pinned.** Test files (default: `tests/`, `test_*.py`, `conftest.py`; configurable) are held at their HEAD version in every state we build. Edits to test files are excluded from bisection. This matters because the failing test may not exist yet at the good commit, and the agent may have edited tests itself.

### Stage 0 — Precheck

1. Run the test 3 times at HEAD and 3 times at the good commit (tests pinned).
2. The good commit must pass 3/3, or abort with "the test fails without the agent's changes."
3. HEAD failing 3/3 = stable mode, 1 run per state. HEAD failing 1–2 of 3 = flaky mode: 5 runs per state, and the report says so. A state counts as TARGET\_FAIL if *any* run fails the target way (a failure is evidence; passes never prove a flaky bug absent), PASS only if every run passes. A majority vote was the original plan, but the real recorded cache bug fails about 28% of the time, so a majority calls the buggy state PASS almost every time.
4. Save HEAD's failure signature for TARGET\_FAIL matching.

### Stage 1 — Find the guilty commit (blame, then bisect)

1. **Blame hint:** `git blame` on `--line` at HEAD gives commit C.
2. **Check C directly:** if C's parent passes and C fails, C is guilty after just 2 runs.
3. **Otherwise** run `git bisect` over good..HEAD, mapping OTHER to `skip`.
4. Read the Entire checkpoint trailer on the guilty commit G. No trailer means a human wrote it: report the commit and stop.

### Stage 2 — Rebuild the session as replayable edits

1. Load the session(s) linked to G's checkpoint. Concurrent sessions are merged by timestamp.
2. The agent adapter turns the transcript into ordered Steps. Each holds the edit, the reasoning text right before it, the tool output right before that (the trigger), and the user prompt in effect.
3. Full-file writes are split into hunks against the file's previous content, so each hunk is its own edit.
4. **Replay fidelity check:** apply all edits in order to G's parent and diff against G, ignoring pinned tests. Any leftover difference, from bash commands like `sed` or from human edits, becomes one synthetic "unattributed" edit at the end, flagged in the report.
5. **Dependency map:** edit B depends on an earlier edit A in the same file if, with A removed, B's target text no longer matches exactly once. These are pure string checks with no test runs, and roughly 50 edits means about 1,000 cheap checks.

### Stage 3 — Edit-level blame hint

1. Map `--line` back through the replay to the edit(s) that last wrote it.
2. Revert those edits plus everything that depends on them from G's state, then run the test.
3. PASS means it's a candidate root cause, so run P(k−1) and P(k) from Stage 5 to confirm the pass-to-fail boundary (2 runs), then skip to Stage 7. All three root-cause facts are then proven. Still TARGET\_FAIL means blame was wrong (the report says "NOT the cause"), so continue.

### Stage 4 — Graph pruning (verified, never trusted)

1. Ask Entire Graph which files are reachable from the failing test. Edits to unreachable files are prune candidates. An edit can't be pruned if a kept edit depends on it.
2. **One verification run:** G with every prune candidate reverted. If it's still TARGET\_FAIL, pruning is safe. If not, drop pruning entirely, because Graph missed a dependency such as config or a dynamic import.
3. Log the run count with and without pruning; that's the token-efficiency metric.

### Stage 5 — Prefix bisect

1. Define prefix state P(k) = G's parent + the first k kept edits, in order. Prefixes always apply cleanly because they respect the original order, so dependencies are never a problem here.
2. P(0) passes (Stage 0) and P(n) fails (verified in Stage 4, or at G), so a pass-to-fail boundary is guaranteed to exist.
3. Binary search for k where P(k−1) is PASS and P(k) is TARGET\_FAIL. An OTHER result means testing the nearest untested neighbour instead.
4. **Parallel version:** with W sandboxes, test W evenly spaced points per round. Rounds ≈ log base (W+1) of n: about 2 rounds for 15 edits with 4 workers, versus 4 sequential runs.
5. **Caveat:** if the test flips back and forth across the session, this finds *a* boundary, not necessarily the first. For sessions under 20 edits, a `--scan` flag tests every prefix to be sure.

### Stage 6 — Confirm from the end

1. Revert edit k plus its dependents from the final state (G, minus pruned edits) and run the test.
2. **PASS:** root cause confirmed. Report k, plus any dependent edits reverted alongside it as collateral.
3. **Still TARGET\_FAIL:** there's more than one independent cause. Run ddmin (delta debugging) over the kept edits to find a minimal set whose revert makes the test pass, keeping every tried set closed under dependents. Cap at 20 runs, and report the set or "inconclusive."

### Stage 7 — Reasoning and lesson

1. Pull the guilty step's trigger, reasoning, prompt, and diff from Stage 2.
2. Query Brain for an existing lesson on the same files or error. Reuse it if one matches.
3. Otherwise Gemini writes a one-sentence, imperative lesson from the trigger, reasoning, diff and failing output, general enough to apply beyond this exact line.
4. If the agent's reasoning wasn't recorded (hidden thinking), say so and build the lesson from the trigger and diff alone.

### Stage 8 — Counterfactual reruns

**Setup:**

1. Start state = P(k−1), the code right before the guilty edit, with test files pinned and read-only.
2. Task prompt = the original user prompt + a generated summary of edits 1 to k−1 + "continue the task."
3. Two arms using the **same rerun agent**, N runs each (default 5): A has no lesson, B has the lesson injected into context. Each run is capped at 15 agent turns and 5 minutes.

**A run succeeds only if** the target test passes, every test that passed at G still passes, and the test files are unchanged. Without the second check, an agent could "fix" the test by deleting the feature; without the third, by editing the test. A run that touched tests counts as a failure, labelled "tampered."

**Decision, checked in this order:**

| Result | Condition | Action |
| --- | --- | --- |
| Not needed | Arm A passes 4/5 or more | Original failure was a fluke; don't save |
| Validated | One-sided Fisher exact test, p < 0.05 | Save lesson to Brain |
| Promising | B beats A, p ≥ 0.05 | Show it, don't save; suggest `--runs 10` |
| Ineffective | B ≤ A | Lesson doesn't help; stretch: regenerate once and retest |

With 5 runs per arm, only three results reach p < 0.05: 5/5 vs 0/5 (p = 0.004), 4/5 vs 0/5 (p = 0.024), and 5/5 vs 1/5 (p = 0.024). That's why `--runs` is configurable.

**Honest limits:**

- The rerun agent may differ from the original (say, Gemini CLI rerunning a Claude Code session). Both arms use the same agent, so the comparison is fair, but the claim is "this lesson fixes the rerun agent," and the report says which agent ran.
- Reruns start fresh with a summary, not the agent's exact mid-session memory.

## Data model and module interfaces

Four modules meet at a few shared types, so each can be built and tested on its own against a hand-written fixture of Steps.

```python
@dataclass
class Step:                  # one replayable agent edit
    id: int                  # order within the session
    session_id: str
    agent: str               # "claude-code", "gemini-cli", ...
    file: str
    kind: str                # "edit" | "write_hunk" | "unattributed"
    old_text: str            # text replaced ("" for new file)
    new_text: str
    reasoning: str | None    # assistant text right before the tool call
    trigger: str | None      # tool output right before that, e.g. a traceback
    prompt: str              # user prompt in effect

@dataclass
class State:                 # a code snapshot we can test
    base_commit: str         # usually G's parent
    step_ids: list[int]      # edits applied, always in original order
    pinned_tests_from: str   # commit whose test files are copied in (HEAD)

@dataclass
class RunResult:
    outcome: str             # "PASS" | "TARGET_FAIL" | "OTHER"
    signature: str
    exit_code: int
    duration_ms: int
    tokens: int | None       # agent reruns only

@dataclass
class RootCause:             # stage 6 → stages 7–8
    step: Step               # guilty edit (reasoning, trigger, prompt, diff)
    collateral: list[int]    # dependents reverted alongside it
    before_state: State      # P(k-1): rerun start state
    edits_before: list[Step] # for the "summary of edits 1..k-1" prompt
    failing_output: str
    head_state: State        # for "everything that passed at G still passes"
```

| Module | Main functions | Input to output |
| --- | --- | --- |
| `adapters/` | `load_session(commit)`, `parse(transcript, agent)` | Commit → list of Steps |
| `replay/` | `build(state)`, `run(state, cmd, n)` | State → RunResults |
| `bisect/` | `deps(steps)`, `prune(...)`, `prefix_bisect(...)`, `confirm(...)`, `ddmin(...)` | Steps + `run` → root-cause Step IDs |
| `rerun/` | `make_lesson(step)`, `rerun(state, prompt, lesson, n)`, `decide(a, b)` | Root cause → lesson + verdict |
| `entire.py` | `checkpoint_id(commit)`, `graph_reachable(test)`, `brain_lookup(files, error)`, `brain_save(lesson)` | Entire CLI/MCP → plain Python data |
| `cli.py` | `main()` | Wires stages 0–8, prints the report |

**Contract rules:** `bisect/` only ever talks to code through `replay.run`, so it can be tested against a fake `run` that returns scripted outcomes. `rerun/` only ever talks to Brain through `entire.brain_lookup` / `entire.brain_save`, so it can be tested against an in-memory stub.

## Sandbox and execution environment

Every test run and every agent rerun happens in a throwaway Docker container, so a bad state or a reckless agent can never touch the host or another run.

- **Image:** built once per analysis from the repo's Dockerfile, or a Python base image + `--setup`. It's built at HEAD so dependencies cover the final code.
- **Known limit:** if the agent changed dependencies mid-session (edited `requirements.txt`), earlier states run with HEAD's dependencies. The report flags any session that touched dependency files.
- **Building a state:** `git worktree add` at the base commit, apply the state's edits as string replacements, copy in pinned tests from HEAD, then mount it into the container. No commits are ever made on the user's branches.
- **Test runs:** network off, 120 s timeout (timeouts count as OTHER), container deleted after.
- **Agent reruns:** network limited to the model API, test files mounted read-only, and a diff check afterwards to catch any tampering.
- **Parallelism:** a worker pool of W containers. That's 2–4 on a laptop and 8+ on a Vultr VM. Bisect rounds and rerun arms both fan out across the pool.
- **Result cache:** results are keyed by the hash of the State, so no state is ever tested twice across stages.

## Integrations

Each outside service does real work in the pipeline:

| Service | What it does in agentwhy | Stage |
| --- | --- | --- |
| Entire | Sessions are the input data (each commit's checkpoint trailer links to the agent session behind it); Graph prunes the search; Brain stores and serves validated lessons; Trails receives the root cause as a finding (`--trail`) | 1, 2, 4, 7 |
| Gemini | Writes lessons; Gemini CLI is one of the rerun agents. Claude is the fallback for every model call | 7, 8 |
| Tiger Data | Hypertable of every run (time, analysis, stage, state hash, outcome, duration, tokens); a continuous aggregate charts runs and tokens per stage, showing what Graph pruning saves | All |
| Vultr | A VM hosts the sandbox pool so bisect rounds and rerun arms run in parallel instead of one at a time on a laptop | 5, 8 |

Setup steps for Vultr and Tiger Data are in `docs/setup.md`; the schema is `docs/tiger_schema.sql`.

## Example scenarios

The example repo is a small payments-API client (around 10 files, pytest suite) with Entire enabled from the first commit. Its bugs were made by real agents during recorded Entire sessions; see `docs/demo.md`.

| Scenario | Agent task (the lure) | What goes wrong | What it shows |
| --- | --- | --- | --- |
| 1. Obvious | "Add pagination to `list_invoices`" | Changes what `list_invoices()` returns, on the line the traceback points at | Blame hint confirmed in 1 run, no bisect needed |
| 2. Sneaky (as recorded: "2b") | "Make the smoke script fail fast; also improve the 'gave up' error" | Sets `MAX_RETRIES = 1` globally in `config.py` via `sed -i` (after user steering); the traceback points at the rewritten error line in `client.py` | Blame is wrong, bisect finds the real cause in another file |
| 3. Flaky | "Add response caching" | Time-based cache expiry makes a test fail some of the time | Flaky mode, any-failure voting, honest uncertainty |

Sessions should have about 15 edits, so bisect has real work to do. If an agent won't make a mistake, a hand-written transcript must be labelled as such; never pass a staged session off as real.

## Risks, assumptions, open questions

The biggest dependency is that Entire's transcripts store full edit contents; Claude Code's do.

| Risk | Likelihood | Fallback |
| --- | --- | --- |
| Transcripts lack the before/after text of each edit | Low (Claude Code's own logs have it) | Read Claude Code's local JSONL logs directly, using Entire only for the commit link |
| Replay doesn't reproduce the commit | Medium | The leftover diff becomes an "unattributed" edit (Stage 2), so the pipeline still runs |
| Graph or Brain has no usable CLI/MCP access | Medium | Graph: skip pruning, use plain bisect. Brain: store lessons in Tiger Data, sync to Brain if possible |
| Agent won't make the planted mistake | Medium | Stronger lures, multiple attempts, or a disclosed hand-written transcript |
| Baseline rerun arm passes too often to show a difference | Medium | Raise `--runs`; the verdict reports "Not needed" when the original failure was a fluke |
| Gemini CLI rate limits during reruns | Medium | Cache every run; fall back to Claude Code headless |
| Dependency changes mid-session break early states | Low | Flagged in the report |
