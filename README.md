# whyagent

`git blame` for agent decisions: finds which agent edit broke a test, proves it by experiment, and validates the lesson. Built on [Entire](https://entire.io). See `whyagent — PRD.md`.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                 # unit tests
.venv/bin/whyagent --check-entire   # smoke-test the Entire connection
```

## Usage

```sh
whyagent --test "pytest tests/test_auth.py::test_login" --good a1b2c3d --line src/auth.py:42
```

## Layout

| Module | Owner | Status |
| --- | --- | --- |
| `whyagent/types.py` | shared | Step / State / RunResult |
| `whyagent/entire.py` | shared | Entire CLI wrapper + connection check |
| `whyagent/adapters/` | Person 1 | Claude Code JSONL parser done; Entire transcript fetch TODO |
| `whyagent/replay/` | Person 2 | git-worktree build, pinned tests, signatures, cache (local, Docker TODO) |
| `whyagent/bisect/` | Person 3 | deps, prefix bisect, confirm, ddmin (Graph pruning TODO) |
| `whyagent/rerun/` | Person 4 | Fisher test + verdict done; Gemini lesson + reruns TODO |
| `whyagent/cli.py` | Person 4 | Stages 0–1 wired; 2–8 TODO |
