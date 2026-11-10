# CLAUDE.md

Context for Claude Code sessions in this repo. The full spec is `whyagent — PRD.md`; read it before any non-trivial change.

## What this is

**agentwhy**: `git blame` for agent decisions. When an AI coding agent breaks a test, agentwhy finds which agent *edit* caused it, proves it with real test runs (not an LLM's opinion), shows the agent's reasoning and the error it was reacting to, then validates a lesson with counterfactual reruns before saving it.

## Pipeline (PRD stages)

0. Precheck: HEAD fails, `--good` passes; stable vs flaky mode; save failure signature.
1. Guilty commit: blame hint on `--line`, else `git bisect`. Read the Entire checkpoint trailer.
2. Load the Entire session(s) for that commit → ordered `Step`s; replay fidelity check; dependency map.
3. Edit-level blame hint (can report the blamed line's edit as "NOT the cause").
4. Entire Graph pruning, always verified with one run.
5. Prefix bisect over edits.
6. Confirm by reverting the edit from the final state; ddmin if multiple causes.
7. Reasoning + lesson (Gemini), check Brain first.
8. Counterfactual reruns (with vs without lesson, Fisher exact test); save to Brain only if validated.

Every test run has three outcomes: `PASS`, `TARGET_FAIL` (same signature as HEAD), `OTHER` (never counts as evidence). Test files are pinned to HEAD in every state.

## Code layout

Shared types live in `whyagent/types.py` (`Step`, `State`, `RunResult`, `RootCause`). Code against them.

- **Find the cause (stages 1–6):** `whyagent/entire.py` (incl. Graph + Brain wrappers), `whyagent/adapters/`, `whyagent/bisect/`, `whyagent/graph.py`, `whyagent/pipeline.py`.
- **Prove the fix (stages 0, 7, 8):** `whyagent/replay/` (Docker sandbox, worker pool, cache, flaky voting), `whyagent/rerun/` (lesson, rerun harness), `whyagent/tiger.py`.
- Contracts: `bisect/` only touches code through `replay.run`; `rerun/` only touches Brain through `entire.brain_lookup` / `brain_save`; stage 6 hands stages 7–8 a `RootCause`.

See the README's layout table for every module.

## Cross-platform

agentwhy runs on Mac, Linux and **Windows**, so code must work on all of them:

- Always decode subprocess output with `encoding="utf-8", errors="replace"`, never bare `text=True` (Windows defaults to cp1252 and crashes on Entire's `●`/`✓` output).
- Don't hard-code `python3` (on Windows it's a Microsoft Store stub). Use `sys.executable` in tests.
- Venv executables are `.venv/bin/...` on Mac/Linux and `.venv\Scripts\...` on Windows.

## Setup

```sh
# Mac/Linux                              # Windows (PowerShell)
python3 -m venv .venv                    python -m venv .venv
.venv/bin/pip install -e '.[dev]'        .venv\Scripts\pip install -e '.[dev]'
.venv/bin/pytest -q                      .venv\Scripts\pytest -q
.venv/bin/agentwhy --check-entire        .venv\Scripts\agentwhy --check-entire
```

Infra (Vultr VM, Tiger Data) is in `docs/setup.md`.

## Entire and git workflow

- `origin` is the **Entire** repo (`entire://aws-us-east-2.entire.io/et/agentwhy/agentwhy`). Push there so session checkpoints upload. A `github` remote (`https://github.com/Crystalge07/AgentWhy.git`) holds a code-only copy; push to it with `git push github main`.
- Each clone must run `entire enable --agent claude-code` once (git hooks are local). Claude Code sessions started *before* that aren't recorded; restart Claude Code.
- Commits made in a Claude Code session get an Entire checkpoint trailer linking to the session. That trailer is agentwhy's input data, so don't strip it.
- **Never force-push `main`.** Work on a branch (`<name>/<topic>`) and merge in.
- Session transcripts upload with every push to `origin`. Never paste API keys or secrets into prompts. Keep `GEMINI_API_KEY`, `TIGER_DATABASE_URL`, etc. in env vars, never in committed files.
