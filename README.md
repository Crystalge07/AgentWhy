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
| `whyagent/types.py` | shared | Step / State / RunResult (+ RootCause TODO) |
| `whyagent/entire.py` | A | Entire CLI wrapper + connection check; Graph/Brain TODO |
| `whyagent/adapters/` | A | Claude Code JSONL parser + Entire checkpoint load done; session merge, replay fidelity TODO |
| `whyagent/bisect/` | A | deps, prefix bisect, confirm, ddmin done; Stage 3 hint, Graph pruning TODO |
| `whyagent/replay/` | B | git-worktree build, pinned tests, signatures, cache (local); Docker, worker pool, Tiger logging TODO |
| `whyagent/rerun/` | B | Fisher test + verdict done; Gemini lesson + reruns TODO |
| `whyagent/cli.py` | A: stages 1–7, B: stages 0, 8 | Stages 0–1 wired; 2–8 TODO |

**A** = Entire + causal analysis (also owns the seeded demo repo and recorded sessions). **B** = execution + agents (also owns Vultr and Tiger Data). Infra setup: `docs/setup.md`.
