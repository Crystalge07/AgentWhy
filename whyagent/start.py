"""Guided start: `agentwhy` with no --test finds the failing tests, asks which one, finds a good commit and runs.

Two pieces, usable on their own:
- failing_tests: run the suite once at HEAD (in the sandbox) and list the pytest node ids that fail.
- find_good: the nearest commit where the test passes (tests pinned to HEAD, like every other state). It tries
  the commit just before the agent's work first (the parent of the oldest run of Entire-checkpointed commits
  ending at HEAD), then HEAD~1, ~2, ~4, … so a long history costs a handful of runs.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys

from . import entire
from .types import PASS, State

SUITE_CMD = "{py} -m pytest -q -p no:cacheprovider -rf --tb=no"
TEST_CMD = "{py} -m pytest -q -p no:cacheprovider {test}"


def _py(sandbox) -> str:
    """`python` in the Docker image; on the host it may not be on PATH (macOS, Windows' Store stub)."""
    return "python" if getattr(sandbox, "name", "") == "docker" else shlex.quote(sys.executable)
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)", re.MULTILINE)
MAX_BACK = 64  # find_good gives up past HEAD~64


def _git(*a) -> str:
    return subprocess.run(["git", *a], capture_output=True, encoding="utf-8", errors="replace",
                          check=True).stdout.strip()


def parse_failed(output: str) -> list[str]:
    """pytest -rf summary lines ("FAILED tests/x.py::test_y - AssertionError") → unique node ids, in order."""
    return list(dict.fromkeys(m.group(1) for m in _FAILED.finditer(output)))


def failing_tests(sandbox, head: str, timeout: int = 900) -> tuple[list[str], str]:
    """Run the whole suite once at HEAD in `sandbox`. Returns (failing node ids, output tail)."""
    from .replay import Replayer

    cmd = SUITE_CMD.format(py=_py(sandbox))
    rp = Replayer(".", [], cmd, sandbox=sandbox)
    d = rp.build(State(head, [], head))
    try:
        e = sandbox.exec(d, cmd, timeout)
    finally:
        rp.teardown(d)
    out = e.output or ""
    return parse_failed(out), "\n".join(out.strip().splitlines()[-8:])


def candidates(head: str = "HEAD", max_back: int = MAX_BACK) -> list[tuple[str, str]]:
    """Commits to try as the good commit, nearest-first: (sha, why)."""
    chain = _git("rev-list", "--first-parent", f"--max-count={max_back + 1}", head).splitlines()
    out: list[tuple[str, str]] = []
    # The agent's work: the newest run of Entire-checkpointed commits, maybe under a few human ones.
    marked = [bool(entire.checkpoint_id(c)) for c in chain]
    first = next((i for i, m in enumerate(marked) if m), None)
    if first is not None:
        last = first
        while last + 1 < len(chain) and marked[last + 1]:
            last += 1
        if last + 1 < len(chain):
            out.append((chain[last + 1], f"just before the agent's {last - first + 1} checkpointed commit(s)"))
    k = 1
    while k < len(chain):
        out.append((chain[k], f"HEAD~{k}"))
        k *= 2
    if 1 < len(chain) < max_back + 1:  # reached the root commit
        out.append((chain[-1], f"HEAD~{len(chain) - 1} (oldest)"))
    seen: set[str] = set()
    return [(c, why) for c, why in out if not (c in seen or seen.add(c))]


def find_good(test: str, sandbox, head: str, say=print) -> str | None:
    """The first candidate where `test` passes once (precheck re-checks it 5 times). None if none pass."""
    from .replay import Replayer

    rp = Replayer(".", [], test, sandbox=sandbox)
    rp.stage = "find-good"
    for sha, why in candidates(head):
        res = rp.run(State(sha, [], head), 1)[0]
        ok = res.outcome == PASS
        say(f"  {sha[:7]} ({why}) → {'passes ✓' if ok else 'fails'}")
        if ok:
            return sha
    return None


def _ask(prompt: str, default: str = "") -> str:
    try:
        ans = input(prompt).strip()
    except EOFError:
        return default
    return ans or default


def guided(args, make_sandbox) -> bool:
    """Fill args.test / args.good interactively. False if the user stopped or nothing could be found."""
    head = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    branch = "a detached HEAD" if branch == "HEAD" else branch
    subject = _git("log", "-1", "--format=%s", head)
    print("agentwhy: find which agent edit broke a test, and prove it\n")
    print(f"repo: {_git('rev-parse', '--show-toplevel')}")
    print(f"HEAD: {head[:7]} on {branch}: {subject}")
    if _git("status", "--porcelain", "--untracked-files=no"):
        print("note: you have uncommitted changes. agentwhy analyzes commits, so it ignores them")
    sandbox = make_sandbox(args, head)
    if sandbox is None:
        return False
    if not args.test:
        print(f"\nRunning the test suite at HEAD to find failing tests ({SUITE_CMD.format(py=_py(sandbox))})…")
        failed, tail = failing_tests(sandbox, head)
        if failed:
            print(f"{len(failed)} failing:")
            for i, t in enumerate(failed, 1):
                print(f"  {i}. {t}")
            print("  c. enter a test command myself")
            pick = _ask("Which test? [1]: ", "1")
            if pick.isdigit() and 1 <= int(pick) <= len(failed):
                args.test = TEST_CMD.format(py=_py(sandbox), test=shlex.quote(failed[int(pick) - 1]))
        else:
            print("No failing pytest tests found." + (f" Last lines:\n{tail}" if tail else ""))
        if not args.test:
            args.test = _ask("Test command (exit 0 = pass, e.g. npm test -- -t 'name'): ")
            if not args.test:
                print("No test, stopping.")
                return False
    print(f"\ntest: {args.test}")
    if not args.good:
        print("Looking for a commit where it passes (tests from HEAD)…")
        args.good = find_good(args.test, sandbox, head)
        if not args.good:
            print(f"It fails at every commit tried, back to HEAD~{MAX_BACK}. Pass --good SHA if you know one.")
            return False
    print(f"good: {args.good[:7]} {_git('log', '-1', '--format=%s', args.good)}")
    agent = [c for c in _git("rev-list", f"{args.good}..{head}").splitlines() if entire.checkpoint_id(c)]
    print(f"agent commits in between: {len(agent)} with an Entire checkpoint" if agent else
          "note: no commit between good and HEAD has an Entire checkpoint, so there's no recorded agent session "
          "to explain (was `entire enable` run before the agent worked?)")
    shown = f"agentwhy --test {shlex.quote(args.test)} --good {args.good[:7]}"
    print(f"\nSame as: {shown}")
    print("Next: precheck → find the commit → find the edit → confirm → lesson → reruns → PR "
          f"(report: {args.report})")
    if _ask("Start? [Y/n]: ", "y").lower() not in ("y", "yes"):
        return False
    args.command = shown
    return True


def auto_good(args, make_sandbox) -> bool:
    """--test without --good: find the good commit, no questions."""
    head = _git("rev-parse", "HEAD")
    sandbox = make_sandbox(args, head)
    if sandbox is None:
        return False
    print("good: not given, looking for a commit where the test passes…")
    args.good = find_good(args.test, sandbox, head)
    if not args.good:
        print(f"good: the test fails at every commit tried, back to HEAD~{MAX_BACK}. Pass --good SHA.",
              file=sys.stderr)
        return False
    print(f"good: {args.good[:7]}")
    return True
