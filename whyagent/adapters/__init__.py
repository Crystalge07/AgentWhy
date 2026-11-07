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
    per_session: list[list[Step]] = []
    for i, sess in enumerate(meta.get("sessions", [])):
        raw = subprocess.run(["entire", "checkpoint", "explain", cp, "--transcript", "--session-index", str(i)],
                             cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=True).stdout
        per_session.append(parse(raw, sess.get("agent", "claude-code").lower().replace(" ", "-"),
                                 sess.get("session_id", "")))
    return merge_sessions(per_session)


def _git_file(rev: str | None, path: str, cwd: str) -> str | None:
    if rev is None:
        return None
    import subprocess

    p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=cwd, capture_output=True)
    return p.stdout.decode("utf-8", errors="replace") if p.returncode == 0 else None


def load_replayable(commit: str, cwd: str = ".", is_test=None):
    """Commit -> (Steps that replay G's parent exactly into G, Fidelity). PRD Stage 2."""
    import subprocess

    from ..replay import is_test_file
    from .fidelity import reconcile, split_writes

    is_test = is_test or is_test_file

    def git(*a):
        p = subprocess.run(["git", *a], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
        return p.stdout.strip() if p.returncode == 0 else None

    sha = git("rev-parse", commit)
    parent = git("rev-parse", f"{sha}^")  # None for a root commit
    changed = (git("diff", "--name-only", parent or "4b825dc642cb6eb9a060e54bf8d69288fbee4904", sha) or "").split()
    steps = split_writes(load_session(sha, cwd), lambda f: _git_file(parent, f, cwd))
    return reconcile(steps, lambda f: _git_file(parent, f, cwd), lambda f: _git_file(sha, f, cwd), changed, is_test)


def merge_sessions(per_session: list[list[Step]]) -> list[Step]:
    """Interleave concurrent sessions' edits by timestamp and renumber 1..n.

    Within a session the original order always wins (ties and missing timestamps keep it).
    """
    keyed = []
    for si, steps in enumerate(per_session):
        last_ts = ""
        for oi, s in enumerate(steps):
            # non-decreasing per session, so an edit with a missing or skewed timestamp never jumps ahead
            last_ts = max(last_ts, s.ts or "")
            keyed.append(((last_ts, si, oi), s))
    out = [s for _, s in sorted(keyed, key=lambda kv: kv[0])]
    for i, s in enumerate(out, 1):
        s.id = i
    return out
