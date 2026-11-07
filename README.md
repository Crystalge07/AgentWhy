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
| `whyagent/entire.py` | A | Entire CLI wrapper + connection check; Brain (`brain_lookup` / `brain_save`, local store: Entire Brain has no CLI access) |
| `whyagent/adapters/` | A | Claude Code parser, multi-session merge, write hunks, replay fidelity + unattributed edits (byte-exact on real sessions) |
| `whyagent/bisect/` | A | deps, Stage 3 blame hint, prefix bisect (+ parallel, `--scan`), confirm, ddmin |
| `whyagent/graph.py` | A | Stage 4 pruning from a static import graph (Entire Graph has no CLI access), verified with one run |
| `whyagent/pipeline.py` | A | Stages 2–6 end to end → `RootCause` (+ `root_cause.json` for B) |
| `whyagent/replay/` | B | git-worktree build, pinned tests, signatures, cache, Docker sandbox (`--sandbox docker`, default); worker pool, Tiger logging TODO |
| `whyagent/rerun/` | B | Fisher test + verdict done; Gemini lesson (Stage 7) + reruns TODO |
| `whyagent/cli.py` | A: stages 1–6 + report, B: stages 0, 7, 8 | Stages 0–6 wired (verified on real recorded sessions); 7–8 TODO |

**A — Find the cause:** Entire data in, proven root-cause edit out (stages 1–6); also Graph, Brain, the seeded demo repo, and recording scenarios 1 and 3. **B — Prove the fix:** sandbox, precheck, lesson and reruns (stages 0, 7, 8); also Vultr, Tiger Data, Gemini, and recording scenario 2. Details and hour estimates are in the PRD's team-split section. Infra setup: `docs/setup.md`.
