"""Turn parsed Steps into a replay that reproduces the guilty commit exactly (PRD Stage 2.3-2.4).

- Full-file writes become hunks against the file's real previous content, each with just enough
  context that its old_text matches exactly once at the moment it is applied.
- Replaying all edits onto G's parent is diffed against G (tests excluded). Whatever is left over,
  from Bash edits like `sed` or a human tweak before committing, becomes trailing "unattributed"
  edits, so replaying every step always lands exactly on G.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field, replace
from typing import Callable

from ..types import Step

ReadFn = Callable[[str], "str | None"]  # file -> content (None if the file doesn't exist)


class ApplyError(ValueError):
    pass


def occurrences(text: str, sub: str) -> int:
    """Count matches including overlapping ones ("b
b
" occurs twice in "b
b
b
")."""
    n, i = 0, text.find(sub)
    while i != -1:
        n += 1
        i = text.find(sub, i + 1)
    return n


def apply(text: str | None, s: Step) -> str:
    """Apply one hunk-style step. old_text must occur exactly once (or be empty for a new file)."""
    if s.old_text == "":
        if text:
            raise ApplyError(f"step {s.id}: creates {s.file}, but it already has content")
        return s.new_text
    if text is None or occurrences(text, s.old_text) != 1:
        n = 0 if text is None else occurrences(text, s.old_text)
        raise ApplyError(f"step {s.id}: old_text matches {n} times in {s.file}")
    return text.replace(s.old_text, s.new_text, 1)


def hunks(old: str | None, new: str) -> list[tuple[str, str]]:
    """(old_text, new_text) pairs turning `old` into `new`, applied in order, each unique when applied."""
    if not old:
        return [("", new)] if new else []
    cur = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    out, delta = [], 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old.splitlines(keepends=True), b=b,
                                                        autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        s, e = i1 + delta, i2 + delta
        text = "".join(cur)
        k = 0
        while True:
            lo, hi = max(0, s - k), min(len(cur), e + k)
            o = "".join(cur[lo:hi])
            if (o and occurrences(text, o) == 1) or (lo == 0 and hi == len(cur)):
                break
            k += 1
        n = "".join(cur[lo:s] + b[j1:j2] + cur[e:hi])
        out.append((o, n))
        cur[s:e] = b[j1:j2]
        delta += (j2 - j1) - (i2 - i1)
    return out


def split_writes(steps: list[Step], read_base: ReadFn) -> list[Step]:
    """Replace each kind="write" step (new_text = full file) with hunks against the replayed content."""
    cur: dict[str, str | None] = {}
    out: list[Step] = []
    for s in steps:
        if s.file not in cur:
            cur[s.file] = read_base(s.file)
        if s.kind in ("write", "edit_all"):
            new = s.new_text if s.kind == "write" else (cur[s.file] or "").replace(s.old_text, s.new_text)
            for o, n in hunks(cur[s.file], new):
                out.append(replace(s, kind="write_hunk", old_text=o, new_text=n))
            cur[s.file] = new
        else:
            out.append(s)
            try:
                cur[s.file] = apply(cur[s.file], s)
            except ApplyError:
                pass  # left for the fidelity check to report
    for i, s in enumerate(out, 1):
        s.id = i
    return out


@dataclass
class Fidelity:
    matches: bool                      # replaying the agent's own edits reproduces G exactly
    unattributed: list[int] = field(default_factory=list)  # ids of trailing synthetic edits
    dropped: list[int] = field(default_factory=list)       # original ids that couldn't apply
    files: list[str] = field(default_factory=list)         # files with leftover differences
    deleted: list[str] = field(default_factory=list)       # deleted in G; replay leaves them empty
    shell: list[int] = field(default_factory=list)         # leftover edits traced to the agent's shell commands
    shell_guessed: list[int] = field(default_factory=list)  # shell edits where several commands could have made them


def reconcile(steps: list[Step], read_base: ReadFn, read_commit: ReadFn, changed: list[str],
              is_test: Callable[[str], bool]) -> tuple[list[Step], Fidelity]:
    """Replay steps on the parent, diff against G, and append unattributed edits for the leftovers.

    `changed` lists the files that differ between G's parent and G. Test files are ignored on both
    sides, since tests are pinned to HEAD in every state.
    """
    cur: dict[str, str | None] = {}
    kept: list[Step] = []
    dropped: list[int] = []
    for s in steps:
        if is_test(s.file):
            kept.append(s)
            continue
        if s.file not in cur:
            cur[s.file] = read_base(s.file)
        try:
            cur[s.file] = apply(cur[s.file], s)
            kept.append(s)
        except ApplyError:
            dropped.append(s.id)

    files = sorted({f for f in set(changed) | set(cur) if not is_test(f)})
    extra: list[Step] = []
    diff_files, deleted = [], []
    for f in files:
        have = cur[f] if f in cur else read_base(f)
        want = read_commit(f)
        if (have or "") == (want or ""):
            continue
        diff_files.append(f)
        if want is None:
            deleted.append(f)
        for o, n in hunks(have, want or ""):
            extra.append(Step(0, "", "unattributed", f, "unattributed", o, n,
                              reasoning=None, trigger=None, prompt=""))
    out = kept + extra
    for i, s in enumerate(out, 1):
        s.id = i
    ids = [s.id for s in out[len(kept):]]
    return out, Fidelity(matches=not extra and not dropped, unattributed=ids, dropped=dropped,
                         files=diff_files, deleted=deleted)


def _mentions(command: str, file: str) -> bool:
    """Does a shell command name this repo file (by repo path, or by basename as a whole word)?"""
    import re

    cmd = command.replace("\\", "/")
    if file in cmd:
        return True
    base = file.rsplit("/", 1)[-1]
    return re.search(rf"(?<![\w.-]){re.escape(base)}(?![\w.-])", cmd) is not None


# git subcommands that never change a tracked file's content in the working tree
_GIT_NO_WRITE = {"add", "commit", "push", "pull", "fetch", "status", "diff", "log", "show", "ls-files", "grep",
                 "blame", "branch", "tag", "rev-parse", "remote", "config", "check-ignore", "cat-file", "ls-tree"}


def _modifies(command: str, file: str) -> bool:
    """Does some part of the command both name `file` and plausibly change it?

    Splits on && || ; | and newlines, ignores heredoc bodies (commit messages), and skips parts that
    only view files (cat, sed -n, grep...) or are git subcommands that don't touch file content
    (add, commit, diff...). `sed -i`, `git checkout --`, redirects, `mv`, a script run... all count.
    """
    import re

    from .claude_code import _is_view_only

    body = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?\n\1\b", "", command, flags=re.S)  # drop heredocs
    for part in re.split(r"&&|\|\||;|\||\n", body):
        part = part.strip()
        if not part or not _mentions(part, file):
            continue
        # a redirect or tee into the file writes it, whatever the command (echo, cat, printf...)
        if re.search(r"(?<![0-9&])>>?\s*['\"]?(\S*/)?" + re.escape(file.rsplit("/", 1)[-1]) + r"['\"]?(\s|$)", part) \
                or re.search(r"\btee\b", part):
            return True
        if _is_view_only(part):
            continue
        words = part.split()
        if words[0].rsplit("/", 1)[-1] == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub in _GIT_NO_WRITE:
                continue
        return True
    return False


def _content_score(command: str, s: Step) -> int:
    """How many of the edit's added lines appear in the command text (sed replacement, heredoc, echo...)."""
    old = {l.strip() for l in s.old_text.splitlines()}
    added = [l.strip() for l in s.new_text.splitlines() if l.strip() and l.strip() not in old]
    cmd = " ".join(command.split())
    return sum(" ".join(l.split()) in cmd for l in added)


def attribute_shell_edits(steps: list[Step], fid: Fidelity, shell: list[dict]) -> None:
    """Credit leftover (unattributed) edits to the agent shell command that made them.

    Agents often change files with `sed -i`, `git checkout -- f` or a redirect instead of the Edit
    tool. Those are still the agent's decisions: the edit gets kind="shell", the command, and the
    prompt / reasoning / trigger in effect when the agent ran it. Among the commands that modify the
    file, the one whose text contains the edit's added lines wins (the last one on a tie); with no
    content match the last command wins and the edit is listed in `fid.shell_guessed`.
    Edits no command names stay "unattributed" (e.g. a human tweak before committing).
    """
    guessed = []
    for s in steps:
        if s.kind != "unattributed":
            continue
        hits = [c for c in shell if _modifies(c["command"], s.file)]
        if not hits:
            continue
        scores = [_content_score(c["command"], s) for c in hits]
        best = max(scores)
        c = hits[len(scores) - 1 - scores[::-1].index(best)]  # last command with the best score
        if best == 0 and len({h["command"] for h in hits}) > 1:
            guessed.append(s.id)
        s.kind, s.agent, s.command = "shell", "claude-code", c["command"]
        s.session_id, s.ts = c["session_id"], c["ts"]
        s.reasoning, s.trigger, s.prompt = c["reasoning"], c["trigger"], c["prompt"]
    fid.shell = [s.id for s in steps if s.kind == "shell"]
    fid.shell_guessed = guessed
    fid.unattributed = [s.id for s in steps if s.kind == "unattributed"]
