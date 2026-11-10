"""Agent transcript adapters: commit -> list[Step]."""
from __future__ import annotations

from ..types import Step
from .claude_code import parse_claude_code


def parse(transcript: str, agent: str = "claude-code", session_id: str = "", shell_out: list | None = None) -> list[Step]:
    if agent == "claude-code":
        return parse_claude_code(transcript, session_id=session_id, shell_out=shell_out)
    raise NotImplementedError(f"no adapter for agent {agent!r}")


def load_session(commit: str, cwd: str = ".", shell_out: list | None = None) -> list[Step]:
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
                                 sess.get("session_id", ""), shell_out=shell_out))
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
    from .fidelity import attribute_shell_edits, place_shell_edits, reconcile, split_writes

    is_test = is_test or is_test_file

    def git(*a):
        p = subprocess.run(["git", *a], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
        return p.stdout.strip() if p.returncode == 0 else None

    sha = git("rev-parse", commit)
    parent = git("rev-parse", f"{sha}^")  # None for a root commit
    changed = (git("diff", "--name-only", parent or "4b825dc642cb6eb9a060e54bf8d69288fbee4904", sha) or "").split()
    shell: list[dict] = []
    since = _parent_time(parent, cwd)
    read_parent = lambda f: _git_file(parent, f, cwd)
    steps = load_session(sha, cwd, shell_out=shell)
    tree = set(changed) | set((git("ls-tree", "-r", "--name-only", sha) or "").splitlines())
    if parent:
        tree |= set((git("ls-tree", "-r", "--name-only", parent) or "").splitlines())
    fix_paths(steps, tree)
    steps = split_writes(steps, read_parent)
    early = {s.id for s in steps if _before(s.ts, since)}
    for c in shell:
        c["early"] = _before(c.get("ts"), since)
    steps, fid = reconcile(steps, read_parent, lambda f: _git_file(sha, f, cwd), changed, is_test)
    # Entire's transcript is the whole session so far: when the session made earlier commits too, their edits
    # are already in the parent and don't apply to it. That's expected, not a replay problem. (Edits made
    # before the parent commit that *do* apply are kept: agents often edit first and commit in pieces.)
    fid.stale = [i for i in fid.dropped if i in early]
    fid.dropped = [i for i in fid.dropped if i not in early]
    fid.matches = not fid.files and not fid.dropped
    attribute_shell_edits(steps, fid, shell)
    steps = place_shell_edits(steps, fid, read_parent)
    return steps, fid


def _before(ts: str | None, when) -> bool:
    from .claude_code import _when

    t = _when(ts or "") if when else None
    return t is not None and t < when


def _parent_time(parent: str | None, cwd: str):
    """When the parent commit was made (aware datetime), or None."""
    if not parent:
        return None
    import subprocess

    from .claude_code import _when

    p = subprocess.run(["git", "log", "-1", "--format=%cI", parent], cwd=cwd, capture_output=True,
                       encoding="utf-8", errors="replace")
    return _when(p.stdout.strip()) if p.returncode == 0 else None


def fix_paths(steps: list[Step], tree: set[str]) -> None:
    """Map edit paths that aren't repo files onto the repo file they name.

    Paths are made relative to the session's cwd, which is a subdirectory when the agent was started
    (or cd'd) there: `x.py` for `pkg/x.py`, or an absolute path for files outside that subdirectory.
    A path that is a suffix of exactly one repo file (or ends with one) is mapped to it."""
    cache: dict[str, str] = {}
    for s in steps:
        f = s.file
        if not f or f in tree:
            continue
        if f not in cache:
            g = f.replace("\\", "/")
            hits = [t for t in tree if t.endswith("/" + g.lstrip("./")) or g.endswith("/" + t)]
            longest = max((len(h) for h in hits), default=0)
            best = [h for h in hits if len(h) == longest]
            cache[f] = best[0] if len(best) == 1 else f
        s.file = cache[f]


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
