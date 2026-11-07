"""Agent transcript adapters: commit -> list[Step]."""
from __future__ import annotations

from ..types import Step
from .claude_code import parse_claude_code


def parse(transcript: str, agent: str = "claude-code", session_id: str = "") -> list[Step]:
    if agent == "claude-code":
        return parse_claude_code(transcript, session_id=session_id)
    raise NotImplementedError(f"no adapter for agent {agent!r}")


def load_session(commit: str, cwd: str = ".") -> list[Step]:
    """Commit -> Steps via the Entire checkpoint trailer (all sessions, merged in order)."""
    import subprocess

    from .. import entire

    cp = entire.checkpoint_id(commit, cwd)
    if cp is None:
        raise LookupError(f"{commit} has no Entire checkpoint trailer (human commit)")
    meta = entire.explain(cp, cwd) or {}
    steps: list[Step] = []
    for i, sess in enumerate(meta.get("sessions", [])):
        raw = subprocess.run(["entire", "checkpoint", "explain", cp, "--transcript", "--session-index", str(i)],
                             cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=True).stdout
        for s in parse(raw, sess.get("agent", "claude-code").lower().replace(" ", "-"), sess.get("session_id", "")):
            s.id = len(steps) + 1
            steps.append(s)
    return steps
