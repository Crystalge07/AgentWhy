"""Agent transcript adapters: commit -> list[Step]."""
from __future__ import annotations

from ..types import Step
from .claude_code import parse_claude_code


def parse(transcript: str, agent: str = "claude-code", session_id: str = "") -> list[Step]:
    if agent == "claude-code":
        return parse_claude_code(transcript, session_id=session_id)
    raise NotImplementedError(f"no adapter for agent {agent!r}")


def load_session(commit: str, cwd: str = ".") -> list[Step]:
    """Commit -> Steps via the Entire checkpoint trailer.

    TODO(Person 1, hour 0): confirm where Entire stores the full transcript
    (`entire checkpoint explain <id> --json`) and plug it in here.
    """
    from .. import entire

    cp = entire.checkpoint_id(commit, cwd)
    if cp is None:
        raise LookupError(f"{commit} has no Entire checkpoint trailer (human commit)")
    raise NotImplementedError(f"transcript fetch for checkpoint {cp} not wired yet")
