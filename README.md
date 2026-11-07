# whyagent

`git blame` for agent decisions: finds which agent edit broke a test, proves it by experiment, and validates the lesson. Built on [Entire](https://entire.io). See `whyagent — PRD.md`.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                 # unit tests
.venv/bin/whyagent --check-entire   # smoke-test the Entire connection
```

On Windows (PowerShell) use `python -m venv .venv` and `.venv\Scripts\...` in place of `.venv/bin/...`.

## Usage

```sh
whyagent --test "pytest tests/test_auth.py::test_login" --good a1b2c3d --line src/auth.py:42
```

## Layout

| Module | Owner | Status |
| --- | --- | --- |
| `whyagent/types.py` | shared | Step / State / RunResult / RootCause |
| `whyagent/entire.py` | A | Entire CLI wrapper + connection check; Graph + Brain wrappers TODO |
| `whyagent/adapters/` | A | Claude Code JSONL parser + Entire checkpoint load done; session merge, replay fidelity TODO |
| `whyagent/bisect/` | A | deps, prefix bisect, confirm, ddmin done; Stage 3 hint, Graph pruning TODO |
| `whyagent/replay/` | B | git-worktree build, pinned tests, signatures, cache, Docker sandbox (`--sandbox docker`, default), Tiger logging (`whyagent/tiger.py`) done; worker pool TODO |
| `whyagent/rerun/` | B | Fisher test + verdict, Stage 7 Gemini lesson, Stage 8 rerun harness (Gemini CLI in Docker, tamper + regression checks) done; Brain calls wait on `entire.brain_lookup`/`brain_save` |
| `whyagent/cli.py` | A: stages 1–6 + report, B: stages 0, 7, 8 | Stages 0–1 and 7–8 wired (`--root-cause FIXTURE` runs 7–8 alone); 2–6 TODO |

**A — Find the cause:** Entire data in, proven root-cause edit out (stages 1–6); also Graph, Brain, the seeded demo repo, and recording scenarios 1 and 3. **B — Prove the fix:** sandbox, precheck, lesson and reruns (stages 0, 7, 8); also Vultr, Tiger Data, Gemini, and recording scenario 2. Details and hour estimates are in the PRD's team-split section. Infra setup: `docs/setup.md`.
