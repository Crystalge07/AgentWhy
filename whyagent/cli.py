"""whyagent CLI: wires stages 0-8 and prints the report."""
from __future__ import annotations

import argparse
import subprocess
import sys

from . import entire
from .types import PASS, TARGET_FAIL, State


def _git(*a) -> str:
    return subprocess.run(["git", *a], capture_output=True, encoding="utf-8", errors="replace", check=True).stdout.strip()


def cmd_check_entire(_args) -> int:
    ok_all = True
    for name, ok, detail in entire.check_connection():
        ok_all &= ok
        print(f"{'✓' if ok else '✗'} {name:<26} {detail}")
    return 0 if ok_all else 1


def cmd_analyze(args) -> int:
    from .replay import Replayer

    head = _git("rev-parse", "HEAD")
    good = _git("rev-parse", args.good)

    # Stage 0 — precheck (tests pinned from HEAD)
    rp = Replayer(".", [], args.test)
    head_res = rp.run(State(head, [], head), 3)
    good_res = rp.run(State(good, [], head), 3)
    g_pass = sum(r.outcome == PASS for r in good_res)
    h_fail = sum(r.outcome != PASS for r in head_res)
    if g_pass != 3:
        print(f"precheck: good commit passes {g_pass}/3 → abort: the test fails without the agent's changes")
        return 2
    if h_fail == 0:
        print("precheck: HEAD passes 3/3 → nothing to explain")
        return 2
    mode = "stable" if h_fail == 3 else "flaky"
    print(f"precheck: HEAD fails {h_fail}/3, good commit passes 3/3 → {mode}")
    target_sig = next(r.signature for r in head_res if r.outcome != PASS)

    # Stage 1 — guilty commit (blame hint, then git bisect)
    rp = Replayer(".", [], args.test, target_sig=target_sig)
    n = 3 if mode == "flaky" else 1
    guilty = None
    if args.line:
        path, ln = args.line.rsplit(":", 1)
        c = _git("blame", "-L", f"{ln},{ln}", "--porcelain", "HEAD", "--", path).split()[0]
        parent = _git("rev-parse", f"{c}^")
        if rp.outcome(State(parent, [], head), n) == PASS and rp.outcome(State(c, [], head), n) == TARGET_FAIL:
            guilty = c
            print(f"commit: blamed {c[:7]} ✓ confirmed (parent passes, commit fails)")
    if guilty is None:
        commits = _git("rev-list", "--reverse", f"{good}..{head}").split()
        lo, hi = -1, len(commits) - 1  # index -1 == good
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if rp.outcome(State(commits[mid], [], head), n) == PASS:
                lo = mid
            else:
                hi = mid
        guilty = commits[hi]
        print(f"commit: bisected → {guilty[:7]}")

    cp = entire.checkpoint_id(guilty)
    if cp is None:
        print(f"commit {guilty[:7]} has no Entire checkpoint → human-written, stopping")
        return 0
    print(f"checkpoint: {cp}")
    print("stages 2–8: not wired yet (see adapters/, bisect/, rerun/)")
    print(f"test runs used: {rp.runs}")
    return 0


def main(argv=None) -> int:
    # Windows consoles/pipes default to cp1252, which can't print the ✓/✗ marks.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    from dotenv import load_dotenv

    load_dotenv()  # reads .env (gitignored) into the environment
    p = argparse.ArgumentParser(prog="whyagent", description="Find which agent decision broke a test, and prove it.")
    p.add_argument("--test", help="command that runs one failing test (exit 0 = pass)")
    p.add_argument("--good", help="commit where the test passes")
    p.add_argument("--line", help="file:line the failure points at (blame hint)")
    p.add_argument("--runs", type=int, default=5, help="reruns per arm (default 5)")
    p.add_argument("--setup", help="environment setup command")
    p.add_argument("--no-rerun", action="store_true", help="stop after root cause")
    p.add_argument("--check-entire", action="store_true", help="smoke-test the Entire connection and exit")
    args = p.parse_args(argv)
    if args.check_entire:
        return cmd_check_entire(args)
    if not (args.test and args.good):
        p.error("--test and --good are required (or use --check-entire)")
    return cmd_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
