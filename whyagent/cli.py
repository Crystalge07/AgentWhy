"""whyagent CLI: wires stages 0-8 and prints the report."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import entire
from .report import Report, state_label
from .types import PASS, TARGET_FAIL, State


def _git(*a) -> str:
    return subprocess.run(["git", *a], capture_output=True, encoding="utf-8", errors="replace", check=True).stdout.strip()


def cmd_check_entire(_args) -> int:
    ok_all = True
    for name, ok, detail in entire.check_connection():
        ok_all &= ok
        print(f"{'✓' if ok else '✗'} {name:<26} {detail}")
    return 0 if ok_all else 1


def _finish(report: Report, args, code: int) -> int:
    path = report.write(args.report)
    print(f"report: {path}")
    if args.open:
        webbrowser.open(path.resolve().as_uri())
    return code


def cmd_render(args) -> int:
    report = Report()
    report.clear()
    report.update(json.loads(Path(args.render).read_text(encoding="utf-8")))
    return _finish(report, args, 0)


def cmd_analyze(args) -> int:
    from .replay import DockerSandbox, LocalSandbox, Replayer, SandboxError

    head = _git("rev-parse", "HEAD")
    good = _git("rev-parse", args.good)
    report = Report(repo=Path.cwd().name, test=args.test, good=good, head=head, line=args.line)

    def run(rp, stage, state, n):
        res = rp.run(state, n)
        report.log_runs(stage, state_label(state), res)
        return res

    def outcome(rp, stage, state, n):
        res = [r.outcome for r in run(rp, stage, state, n)]
        return max(set(res), key=res.count)

    # Stage 0 — precheck (tests pinned from HEAD)
    try:
        sandbox = DockerSandbox(".", head, setup=args.setup) if args.sandbox == "docker" else LocalSandbox()
        sandbox.prepare()
    except SandboxError as e:
        print(f"sandbox: {e}")
        return 2
    print(f"sandbox: {sandbox.name}" + (f" ({sandbox.image}, network off)" if args.sandbox == "docker" else " (no isolation)"))
    rp = Replayer(".", [], args.test, sandbox=sandbox)
    head_res = run(rp, "precheck", State(head, [], head), 3)
    good_res = run(rp, "precheck", State(good, [], head), 3)
    g_pass = sum(r.outcome == PASS for r in good_res)
    h_fail = sum(r.outcome != PASS for r in head_res)
    mode = "stable" if h_fail == 3 else "flaky"
    report["precheck"] = {"head_fail": h_fail, "good_pass": g_pass, "n": 3, "mode": mode}
    if g_pass != 3:
        print(f"precheck: good commit passes {g_pass}/3 → abort: the test fails without the agent's changes")
        report.note("Aborted: the good commit doesn't pass 3/3, so the test fails without the agent's changes.")
        return _finish(report, args, 2)
    if h_fail == 0:
        print("precheck: HEAD passes 3/3 → nothing to explain")
        report.note("HEAD passes 3/3, nothing to explain.")
        return _finish(report, args, 2)
    print(f"precheck: HEAD fails {h_fail}/3, good commit passes 3/3 → {mode}")
    target_sig = next(r.signature for r in head_res if r.outcome != PASS)
    report["precheck"]["signature"] = target_sig

    # Stage 1 — guilty commit (blame hint, then git bisect)
    rp = Replayer(".", [], args.test, target_sig=target_sig, sandbox=sandbox)
    n = 3 if mode == "flaky" else 1
    guilty = blamed = None
    if args.line:
        path, ln = args.line.rsplit(":", 1)
        c = _git("blame", "-L", f"{ln},{ln}", "--porcelain", "HEAD", "--", path).split()[0]
        blamed = c
        parent = _git("rev-parse", f"{c}^")
        if (outcome(rp, "commit", State(parent, [], head), n) == PASS
                and outcome(rp, "commit", State(c, [], head), n) == TARGET_FAIL):
            guilty = c
            print(f"commit: blamed {c[:7]} ✓ confirmed (parent passes, commit fails)")
    if guilty is None:
        commits = _git("rev-list", "--reverse", f"{good}..{head}").split()
        lo, hi = -1, len(commits) - 1  # index -1 == good
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if outcome(rp, "commit", State(commits[mid], [], head), n) == PASS:
                lo = mid
            else:
                hi = mid
        guilty = commits[hi]
        print(f"commit: bisected → {guilty[:7]}")

    cp = entire.checkpoint_id(guilty)
    report["commit"] = {"sha": guilty, "method": "blame" if guilty == blamed else "bisect", "blamed": blamed,
                        "blame_confirmed": guilty == blamed if blamed else None, "checkpoint": cp, "human": cp is None}
    if cp is None:
        print(f"commit {guilty[:7]} has no Entire checkpoint → human-written, stopping")
        return _finish(report, args, 0)
    print(f"checkpoint: {cp}")

    # Stages 2–6 (A): session -> replayable edits -> blame hint -> pruning -> bisect -> confirm
    from dataclasses import asdict

    from .adapters import load_replayable
    from .pipeline import Options, find_root_cause

    try:
        steps, fid = load_replayable(guilty)
    except Exception as e:  # noqa: BLE001 — report it, don't crash
        print(f"session: couldn't load the Entire session ({e})")
        report.note(f"Couldn't load the Entire session for checkpoint {cp}: {e}")
        return _finish(report, args, 2)
    parent = _git("rev-parse", f"{guilty}^")
    opts = Options(line=args.line, n=n, prune=not args.no_prune, scan=args.scan, workers=args.workers)
    rc = find_root_cause(".", steps, fid, guilty, parent, head, args.test, target_sig, sandbox, opts, report)
    print(f"test runs used: {rp.runs + report.get('runs_total', 0)}")
    if rc is None:
        return _finish(report, args, 1)
    rc_path = Path(args.report).with_name("root_cause.json")
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    rc_path.write_text(json.dumps(asdict(rc), indent=2), encoding="utf-8")
    print(f"root cause handoff: {rc_path}")
    if not args.no_rerun:
        print("stages 7–8 (lesson + counterfactual reruns): B's rerun/, not wired yet")
    return _finish(report, args, 0)


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
    p.add_argument("--setup", help="environment setup command, run once when building the sandbox image")
    p.add_argument("--sandbox", choices=["docker", "local"], default="docker",
                   help="run tests in throwaway Docker containers (default) or directly on the host")
    p.add_argument("--no-rerun", action="store_true", help="stop after root cause")
    p.add_argument("--no-prune", action="store_true", help="skip Graph pruning (for the pruned-vs-blind comparison)")
    p.add_argument("--scan", action="store_true", help="test every prefix instead of bisecting (sessions < 20 edits)")
    p.add_argument("--workers", type=int, default=1, help="prefixes tested per bisect round (default 1)")
    p.add_argument("--check-entire", action="store_true", help="smoke-test the Entire connection and exit")
    p.add_argument("--report", default=".whyagent/report.html", help="where to write the HTML report (+ .json)")
    p.add_argument("--open", action="store_true", help="open the HTML report in a browser when done")
    p.add_argument("--render", metavar="JSON", help="render a saved report .json to HTML and exit")
    args = p.parse_args(argv)
    if args.check_entire:
        return cmd_check_entire(args)
    if args.render:
        return cmd_render(args)
    if not (args.test and args.good):
        p.error("--test and --good are required (or use --check-entire)")
    return cmd_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
