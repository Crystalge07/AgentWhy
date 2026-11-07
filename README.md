# whyagent

`git blame` for agent decisions. When an AI coding agent breaks a test, whyagent finds which agent *edit* caused it, proves it with real test runs (not an LLM's opinion), shows the agent's reasoning and the error it was reacting to, then validates a lesson with counterfactual reruns before saving it. Built on [Entire](https://entire.io), which records agent sessions and links each one to its commit.

> "`git blame` tells you which line changed. We tell you which decision caused it, and prove it."

Full spec: `whyagent — PRD.md`. Demo repo and recordings: `docs/demo.md`. Presentation material: `docs/slides.md`.

## Setup

```sh
# Mac/Linux                              # Windows (PowerShell)
python3 -m venv .venv                    python -m venv .venv
.venv/bin/pip install -e '.[dev]'        .venv\Scripts\pip install -e '.[dev]'
.venv/bin/pytest -q                      .venv\Scripts\pytest -q
.venv/bin/whyagent --check-entire        .venv\Scripts\whyagent --check-entire
```

Needs git, Docker (default sandbox), and the Entire CLI with `entire enable --agent claude-code` run in the analyzed repo. Optional env vars (put them in `.env`, never in committed files): `GEMINI_API_KEY` (Stage 7 lesson + Gemini CLI reruns), `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (Claude Code reruns; the token comes from `claude setup-token` and runs on a Claude subscription), `TIGER_DATABASE_URL` (run log, chart, shared Brain). Infra (Vultr VM, Tiger Data): `docs/setup.md`.

## Usage

Run it inside the repo whose test broke. It needs one failing test and a commit where that test passes:

```sh
whyagent --good f39623e \
  --test "python -m pytest -q -p no:cacheprovider tests/test_client.py::test_retries_server_errors_then_succeeds"
```

```
precheck: HEAD fails 5/5, good commit passes 5/5 → stable
blame hint: payments/client.py:39, from the traceback (pass --line to override)
commit: blamed 943dc4b ✓ confirmed (parent passes, commit fails)
checkpoint: 01M6VNK9RRYABNDW2ZJSV423XS
session: 5fc48f02-… · 5 edits · replay matches commit ✓ with 2 edit(s) made by the agent's shell commands
blame hint: edit #1 touches payments/client.py:39 (payments/client.py) → NOT the cause (revert, still fails)
pruning: 2 edit(s) unreachable from the test → still fails without them ✓ pruned
bisect: 3 edits → 2 test runs → first failing prefix at edit #8
confirm: revert #8 from the final state → test passes ✓ root cause
root cause: edit #8 payments/config.py:9  TIMEOUT_S = 2
command: … sed -i '' 's/^TIMEOUT_S = 10$/TIMEOUT_S = 2/; s/^MAX_RETRIES = 3$/MAX_RETRIES = 1/' …
reasoning: The constructor arguments go, and the defaults move to config. Lowering `MAX_RETRIES` globally will break …
test runs used: 6
```

It writes `.whyagent/report.html` (+ `report.json` and `root_cause.json`).

| Flag | Meaning |
| --- | --- |
| `--test CMD` | Command that runs one failing test (exit 0 = pass). Required |
| `--good SHA` | A commit where the test passes. Required |
| `--line FILE:N` | The line the failure points at; used as the blame hint. Default: the deepest non-test frame in the pytest traceback |
| `--setup CMD` | Setup command for the sandbox image (default: pytest, plus `requirements.txt` and/or `pip install -e .`) |
| `--sandbox docker\|local` | Where tests run (default `docker`, network off). `local` runs on the host |
| `--workers N` | Sandboxes in parallel for bisect rounds and reruns (default 2; 4+ on a Vultr VM) |
| `--no-prune` | Skip Graph pruning (for the pruned-vs-blind comparison) |
| `--scan` | Test every prefix instead of bisecting (sessions under 20 edits; finds flip-flops) |
| `--no-rerun` | Stop after the lesson; skip the counterfactual reruns |
| `--runs N` | Reruns per arm in Stage 8 (default 5) |
| `--rerun-agent gemini-cli\|claude-code` | Agent for the reruns (same agent in both arms) |
| `--lesson TEXT` | Test this lesson instead of asking Gemini to write one |
| `--trail` | Post the root cause as a finding on this branch's Entire trail (proof, reasoning, lesson), with the revert as a suggested patch |
| `--root-cause JSON` | Run stages 7–8 only, from a saved `root_cause.json` (pass `--test`) |
| `--report PATH` / `--open` | Where to write the HTML report; open it when done |
| `--render JSON` | Re-render a saved `report.json` to HTML |
| `--chart` | Write the Tiger Data pruning chart (`.whyagent/chart.html`) |
| `--check-entire` | Smoke-test the Entire connection |

## How it works

| Stage | What it does |
| --- | --- |
| 0 Precheck | HEAD fails, `--good` passes (5 runs each). Stable or flaky mode; saves the failure signature. Flaky: measures the failure rate (15 runs) and sizes runs per state so a buggy state shows the bug 95% of the time |
| 1 Guilty commit | Blame hint on `--line`, checked directly (skipped if it's older than `--good`); else `git bisect` (test-only commits skipped). Reads the Entire checkpoint trailer |
| 2 Session | Loads the Entire session(s) as ordered edits, credits `sed -i` / `git checkout --` edits to the agent's shell commands, and checks that replaying them rebuilds the commit |
| 3 Blame hint | Reverts the edit behind `--line`: passes → confirmed; still fails → "NOT the cause" |
| 4 Pruning | Drops edits the test can't import, always verified with one run |
| 5 Bisect | Prefix bisect over the agent's edits, in parallel on the worker pool |
| 6 Confirm | Reverts the edit from the final state → test passes. ddmin if there's more than one cause |
| 7 Lesson | Brain first; otherwise Gemini writes a one-sentence lesson from the reasoning, trigger and diff (or `--lesson`) |
| 8 Reruns | The agent redoes the task from just before the bad edit, with vs without the lesson; Fisher exact test; saved to Brain only if validated |

Every test run is `PASS`, `TARGET_FAIL` (same signature as HEAD) or `OTHER` (never evidence). Test files are pinned to HEAD in every state, so an agent that edited a test can't hide the bug.

## Status (2026-11-07)

- **Stages 0–6 verified end to end** on all three recorded scenarios in the Docker sandbox, on both macOS and Windows: scenario 1 (5 runs), 2b (6 runs, blame NOT the cause), 3 (flaky, 19–44 runs). 77 unit tests.
- **Stages 7–8 are wired and unit-tested, but not yet run end to end with a real model.** Blocked on a Gemini key with quota or an Anthropic API key. `--lesson` lets Stage 8 run without Gemini.
- **Not tried yet:** Vultr VM, the worker pool with more than 2 workers on real sessions, the egress proxy outside the tests.
- Entire Graph and Brain have no CLI access we found: pruning uses a static import graph (`whyagent/graph.py`), and Brain is a local JSONL store (`~/.whyagent/brain.jsonl` or `WHYAGENT_BRAIN`) mirrored to a `lessons` table in Tiger Data, so validated lessons are shared across the team.
- `whyagent --chart` pairs each bug's latest pruned and `--no-prune` analyses from Tiger and compares edit-level test runs (pruning, bisect, confirm).

## Layout

| Module | Owner | What it does |
| --- | --- | --- |
| `whyagent/types.py` | shared | `Step` / `State` / `RunResult` / `RootCause` |
| `whyagent/entire.py` | A | Entire CLI wrapper + connection check; Brain (`brain_lookup` / `brain_save`, local file + Tiger) |
| `whyagent/adapters/` | A | Claude Code parser, multi-session merge, write hunks, replay fidelity, shell-edit attribution |
| `whyagent/bisect/` | A | Dependency map, Stage 3 blame hint, prefix bisect (parallel, `--scan`), confirm, ddmin |
| `whyagent/graph.py` | A | Stage 4 pruning from a static import graph, verified with one run |
| `whyagent/pipeline.py` | A | Stages 2–6 end to end → `RootCause` (+ `root_cause.json`), pruning savings |
| `whyagent/replay/` | B | Worktree builds, pinned tests, signatures, cache, Docker sandbox, worker pool, flaky voting, rerun egress proxy |
| `whyagent/tiger.py`, `chart.py` | B | Tiger Data run log + pruning chart |
| `whyagent/rerun/` | B | Stage 7 Gemini lesson, Stage 8 rerun harness (Gemini CLI or Claude Code in Docker; tamper + regression checks), Fisher test + verdict |
| `whyagent/report.py`, `report.html` | A | JSON + self-contained HTML report |
| `whyagent/cli.py` | A + B | Wires stages 0–8 |
| `demo/check_seeded.py` | A | Checks the seeded demo repo can still produce each bug |

**A, Find the cause** (Anton): stages 1–6, Entire, the demo repo, scenarios 1 and 3. **B, Prove the fix** (Crystal): sandbox, stages 0, 7, 8, Vultr, Tiger Data, Gemini, scenario 2. See the PRD's team-split section.
