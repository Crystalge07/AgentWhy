"""Pure search logic. Talks to code only through a `run(step_ids) -> outcome` callable."""
from __future__ import annotations

from typing import Callable

from ..types import OTHER, PASS, TARGET_FAIL, Step

RunFn = Callable[[list[int]], str]  # applied step ids (in order) -> PASS/TARGET_FAIL/OTHER


def _apply(text: str, s: Step) -> str:
    if not s.old_text:
        return text + s.new_text if text else s.new_text
    return text.replace(s.old_text, s.new_text, 1) if text.count(s.old_text) == 1 else text


def deps(steps: list[Step], base: dict[str, str] | None = None) -> dict[int, set[int]]:
    """B depends on earlier A (same file) if, with A removed, B's old_text no longer matches exactly once.

    `base` maps file -> content at the session's base commit (empty if the file is new).
    """
    base = base or {}
    out: dict[int, set[int]] = {s.id: set() for s in steps}
    by_file: dict[str, list[Step]] = {}
    for s in steps:
        by_file.setdefault(s.file, []).append(s)
    for f, fsteps in by_file.items():
        for j, b in enumerate(fsteps):
            if not b.old_text:
                continue
            for a in fsteps[:j]:
                text = base.get(f, "")
                for s in fsteps[:j]:
                    if s.id != a.id:
                        text = _apply(text, s)
                if text.count(b.old_text) != 1:
                    out[b.id].add(a.id)
    return out


def closure(ids: set[int], dep: dict[int, set[int]]) -> set[int]:
    """ids plus everything that (transitively) depends on them."""
    rev: dict[int, set[int]] = {}
    for b, As in dep.items():
        for a in As:
            rev.setdefault(a, set()).add(b)
    out, stack = set(ids), list(ids)
    while stack:
        for b in rev.get(stack.pop(), ()):
            if b not in out:
                out.add(b)
                stack.append(b)
    return out


def prefix_bisect(order: list[int], run: RunFn) -> int | None:
    """Find index k (1-based) where P(k-1)=PASS and P(k)=TARGET_FAIL. OTHER -> try nearest untested neighbour."""
    known: dict[int, str] = {0: PASS, len(order): TARGET_FAIL}

    def test(k: int) -> str:
        if k not in known:
            known[k] = run(order[:k])
        return known[k]

    lo, hi = 0, len(order)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        cands = sorted(range(lo + 1, hi), key=lambda k: abs(k - mid))
        verdict = None
        for k in cands:
            r = test(k)
            if r != OTHER:
                verdict = (k, r)
                break
        if verdict is None:
            return None  # all OTHER in range: inconclusive
        k, r = verdict
        if r == PASS:
            lo = k
        else:
            hi = k
    return hi


def confirm(final: list[int], k_id: int, dep: dict[int, set[int]], run: RunFn) -> tuple[bool, set[int]]:
    """Revert step k + dependents from the final state. PASS confirms root cause."""
    drop = closure({k_id}, dep)
    return run([i for i in final if i not in drop]) == PASS, drop - {k_id}


def ddmin(final: list[int], dep: dict[int, set[int]], run: RunFn, cap: int = 20) -> set[int] | None:
    """Minimal set of edits whose revert makes the test pass (dependency-closed)."""
    budget = [cap]

    def passes_without(s: set[int]) -> bool:
        if budget[0] <= 0:
            raise StopIteration
        budget[0] -= 1
        drop = closure(s, dep)
        return run([i for i in final if i not in drop]) == PASS

    cur, n = list(final), 2
    try:
        if not passes_without(set(cur)):
            return None
        while len(cur) >= 2:
            size = max(1, len(cur) // n)
            chunks = [cur[i:i + size] for i in range(0, len(cur), size)]
            reduced = False
            for c in chunks:
                if passes_without(set(c)):
                    cur, n, reduced = c, 2, True
                    break
            if not reduced:
                for c in chunks:
                    comp = [x for x in cur if x not in c]
                    if comp and passes_without(set(comp)):
                        cur, n, reduced = comp, max(n - 1, 2), True
                        break
            if not reduced:
                if n >= len(cur):
                    break
                n = min(len(cur), n * 2)
    except StopIteration:
        return None
    return set(cur)


# --- Stage 3: edit-level blame hint ------------------------------------------------------------

def map_line(src_text: str, dst_text: str, line: int) -> int | None:
    """1-based line in src -> the same line in dst, if it survived unchanged (else None)."""
    import difflib

    a, b = src_text.splitlines(), dst_text.splitlines()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal" and i1 < line <= i2:
            return j1 + (line - i1)
    return None


def line_owners(steps: list[Step], file: str, base_text: str | None) -> list[int | None]:
    """For each line of `file` after all steps, the id of the step that last wrote it (None = parent)."""
    import difflib

    from ..adapters.fidelity import apply

    text = base_text
    owners: list[int | None] = [None] * len((text or "").splitlines())
    for s in steps:
        if s.file != file:
            continue
        new = apply(text, s)
        a, b = (text or "").splitlines(), new.splitlines()
        nxt: list[int | None] = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
            nxt += owners[i1:i2] if tag == "equal" else [s.id] * (j2 - j1)
        owners, text = nxt, new
    return owners


def blame_hint(order: list[int], hint: int, dep: dict[int, set[int]], run: RunFn) -> dict:
    """PRD Stage 3. Revert the blamed edit (+ dependents) from the final state.

    PASS -> candidate; confirm the boundary with P(k-1) == PASS and P(k) == TARGET_FAIL (2 runs),
    which proves all three root-cause facts. Anything else -> blame was wrong; go on to bisect.
    """
    drop = closure({hint}, dep)
    reverted = run([i for i in order if i not in drop])
    out = {"step_id": hint, "reverted": sorted(drop), "outcome": reverted, "is_cause": False}
    if reverted != PASS:
        return out
    k = order.index(hint) + 1
    before, after = run(order[:k - 1]), run(order[:k])
    out.update(before=before, after=after, is_cause=before == PASS and after == TARGET_FAIL,
               collateral=sorted(drop - {hint}))
    return out
