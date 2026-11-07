# CLAUDE.md

Context for Claude Code sessions in this repo. The full spec is `whyagent — PRD.md`; read it before any non-trivial change.

## What this is

**whyagent** (placeholder name): `git blame` for agent decisions. When an AI coding agent breaks a test, whyagent finds which agent *edit* caused it, proves it with real test runs (not an LLM's opinion), shows the agent's reasoning and the error it was reacting to, then validates a lesson with counterfactual reruns before saving it.

Pitch: "`git blame` tells you which line changed. We tell you which decision caused it, and prove it."

Built for **HackTrent** (Nov 2026, ~48 h). Main track: **Best Use of Entire**. Also targeting Gemini, Vultr, Tiger Data, and GoDaddy (domain).

## Pipeline (PRD stages)

0. Precheck: HEAD fails, `--good` passes; stable vs flaky mode; save failure signature.
1. Guilty commit: blame hint on `--line`, else `git bisect`. Read the Entire checkpoint trailer.
2. Load the Entire session(s) for that commit → ordered `Step`s; replay fidelity check; dependency map.
3. Edit-level blame hint (the demo's "NOT the cause" moment).
4. Entire Graph pruning, always verified with one run.
5. Prefix bisect over edits.
6. Confirm by reverting the edit from the final state; ddmin if multiple causes.
7. Reasoning + lesson (Gemini), check Brain first.
8. Counterfactual reruns (with vs without lesson, Fisher exact test); save to Brain only if validated.

Every test run has three outcomes: `PASS`, `TARGET_FAIL` (same signature as HEAD), `OTHER` (never counts as evidence). Test files are pinned to HEAD in every state.

## Code layout and ownership

Shared types live in `whyagent/types.py` (`Step`, `State`, `RunResult`, `RootCause`). Code against them.

- **A: Entire + causal analysis:** `whyagent/entire.py`, `whyagent/adapters/`, `whyagent/bisect/`, `cli.py` stages 1–7, seeded demo repo + recorded sessions.
- **B: Execution + agents:** `whyagent/replay/` (Docker sandbox, worker pool, cache), `whyagent/rerun/`, Vultr, Tiger Data, `cli.py` stages 0 and 8.
- Contract: `bisect/` only touches code through `replay.run`.

See the README's layout table for what's done vs TODO.

## Team and machines

Team of four; Crystal (Mac) owns the repo. At least one teammate develops on **Windows**, so code must work on both:

- Always decode subprocess output with `encoding="utf-8", errors="replace"`, never bare `text=True` (Windows defaults to cp1252 and crashes on Entire's `●`/`✓` output).
- Don't hard-code `python3` (on Windows it's a Microsoft Store stub). Use `sys.executable` in tests.
- Venv executables are `.venv/bin/...` on Mac/Linux and `.venv\Scripts\...` on Windows.

## Setup

```sh
# Mac/Linux                              # Windows (PowerShell)
python3 -m venv .venv                    python -m venv .venv
.venv/bin/pip install -e '.[dev]'        .venv\Scripts\pip install -e '.[dev]'
.venv/bin/pytest -q                      .venv\Scripts\pytest -q
.venv/bin/whyagent --check-entire        .venv\Scripts\whyagent --check-entire
```

Infra (Vultr VM, Tiger Data) is in `docs/setup.md`.

## Entire and git workflow

- `origin` is the **Entire** repo (`entire://aws-us-east-2.entire.io/et/agentwhy/agentwhy`). Push there so session checkpoints upload. A `github` remote (`https://github.com/Crystalge07/AgentWhy.git`) holds a code-only copy; push to it with `git push github main`.
- Each machine must run `entire enable --agent claude-code` once after cloning (git hooks are local). Claude Code sessions started *before* that aren't recorded; restart Claude Code.
- Commits made in a Claude Code session get an Entire checkpoint trailer linking to the session. That trailer is whyagent's input data, so don't strip it.
- **Never force-push `main`.** Work on a branch (`<name>/<topic>`) and merge in; merge to main at least every ~6 h.
- Session transcripts upload with every push to `origin`. Never paste API keys or secrets into prompts. Keep `GEMINI_API_KEY`, `TIGER_DATABASE_URL`, etc. in env vars, never in committed files.
