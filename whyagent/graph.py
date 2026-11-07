"""Which files can the failing test reach? (PRD Stage 4: Graph pruning.)

Entire Graph has no CLI or MCP access as of entire 0.x (checked 2026-11-07), so this is the
PRD's fallback: a static Python import graph walked from the test file and its conftest.py
files. It only ever *proposes* prunes. Only .py files it can prove unreachable are candidates,
and the caller verifies every prune with one real test run, so a dynamic import the graph
missed costs one run, never a wrong answer.
"""
from __future__ import annotations

import ast
import posixpath
import re
from typing import Callable

ReadFn = Callable[[str], "str | None"]
SOURCE = "static import graph (Entire Graph has no CLI access)"


def named_test_files(test_cmd: str, files: set[str]) -> list[str]:
    """Test files named in the command, e.g. `pytest tests/test_x.py::test_y -q` -> [tests/test_x.py]."""
    out = []
    for tok in re.split(r"\s+", test_cmd.replace("\\", "/")):
        path = tok.split("::", 1)[0].strip("'\"")
        if path.endswith(".py") and path.lstrip("./") in files:
            out.append(path.lstrip("./"))
    return out


def _module_files(mod: str, files: set[str]) -> list[str]:
    """`a.b.c` -> the repo files that importing it executes (package __init__s and the module)."""
    parts = mod.split(".")
    out = []
    for i in range(1, len(parts) + 1):
        base = "/".join(parts[:i])
        for cand in (f"{base}.py", f"{base}/__init__.py"):
            if cand in files:
                out.append(cand)
    return out


def _imports(path: str, src: str, files: set[str]) -> set[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    pkg = posixpath.dirname(path).replace("/", ".")
    roots = [""] + sorted({f.split("/")[0] for f in files if "/" in f})  # repo root and src/-style layouts
    out: set[str] = set()

    def add(mod: str):
        for root in roots:
            out.update(_module_files(f"{root}.{mod}".strip(".") if root else mod, files))
        # tests often import siblings directly (`from conftest import FakeApi`)
        out.update(_module_files(f"{pkg}.{mod}".strip(".") if pkg else mod, files))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = pkg.split(".") if pkg else []
                base = ".".join(parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts)
                mod = ".".join(p for p in (base, node.module or "") if p)
            else:
                mod = node.module or ""
            if mod:
                add(mod)
            for a in node.names:  # `from pkg import submodule`
                if mod:
                    add(f"{mod}.{a.name}")
    return out


def reachable(test_cmd: str, files: set[str], read: ReadFn) -> set[str] | None:
    """Repo files reachable from the test, or None if the command names no test file (no pruning)."""
    entries = named_test_files(test_cmd, files)
    if not entries:
        return None
    todo = list(entries)
    for e in entries:  # pytest loads every conftest.py from the rootdir down to the test's directory
        d = posixpath.dirname(e)
        while True:
            c = posixpath.join(d, "conftest.py") if d else "conftest.py"
            if c in files:
                todo.append(c)
            if not d:
                break
            d = posixpath.dirname(d)
    seen: set[str] = set()
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        src = read(f)
        if src is not None and f.endswith(".py"):
            todo.extend(_imports(f, src, files) - seen)
    return seen


def prune_candidates(steps, order: list[int], reach: set[str], dep: dict[int, set[int]]) -> list[int]:
    """Edits to .py files the test can't reach, minus any edit a kept edit depends on."""
    by_id = {s.id: s for s in steps}
    cands = {i for i in order if by_id[i].file.endswith(".py") and by_id[i].file not in reach}
    changed = True
    while changed:
        changed = False
        for i in order:
            if i not in cands and dep.get(i, set()) & cands:
                cands -= dep[i]
                changed = True
    return [i for i in order if i in cands]
