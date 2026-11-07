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
