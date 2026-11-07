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
from .types import PASS, TARGET_FAIL, RootCause, State


PRECHECK_RUNS = 5


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


def cmd_chart(args) -> int:
    from . import chart, tiger

    conn = tiger.connect()
    if conn is None:
        print("chart: TIGER_DATABASE_URL isn't set")
        return 2
    with conn:
        conn.execute(tiger.ANALYSES_DDL)
        rows = chart.fetch(conn)
    out = Path(args.report).with_name("chart.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(chart.render(rows), encoding="utf-8")
    print(f"chart: {len(rows)} analyses → {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def cmd_render(args) -> int:
    report = Report()
    report.clear()
    report.update(json.loads(Path(args.render).read_text(encoding="utf-8")))
    return _finish(report, args, 0)


def _make_sandbox(args, head: str):
    from .replay import DockerSandbox, LocalSandbox, SandboxError

    try:
        sandbox = DockerSandbox(".", head, setup=args.setup) if args.sandbox == "docker" else LocalSandbox()
        sandbox.prepare()
    except SandboxError as e:
        print(f"sandbox: {e}")
        return None
    print(f"sandbox: {sandbox.name}" + (f" ({sandbox.image}, network off)" if args.sandbox == "docker" else " (no isolation)"))
    return sandbox


def stages_7_8(rc: RootCause, rp, args, report: Report, runlog) -> int:
    """Stage 7 (lesson) and Stage 8 (counterfactual reruns). rp must know rc's steps."""
    from .rerun import Reruns, agent_by_name, decide, make_lesson

    # Stage 7 — Brain first, then Gemini
    lesson, source = None, "gemini"
    try:
        hit = entire.brain_lookup([rc.step.file], rc.failing_output)  # a validated lesson record, or None
        if hit:
            lesson, source = hit["text"], "brain"
    except Exception as e:  # noqa: BLE001
        print(f"lesson: Brain lookup failed ({e}), writing a new one")
    if getattr(args, "lesson", None):
        lesson, source = args.lesson.strip(), "given"  # --lesson: test a hand-written lesson, skip Gemini
    if not lesson:
        try:
            lesson = make_lesson(rc)
        except Exception as e:
            msg = str(e).split(". {")[0][:200]
            print(f"lesson: Gemini failed ({type(e).__name__}: {msg})")
            report.note(f"Lesson generation failed: {msg}")
            return 1
    hidden = "" if rc.step.reasoning else " (agent reasoning not recorded; built from trigger + diff)"
    print(f'lesson: "{lesson}"' + {"brain": " (from Brain)", "given": " (from --lesson)"}.get(source, "") + hidden)
    report["lesson"] = {"text": lesson, "source": source}
    if args.no_rerun:
        return 0

    # Stage 8 — same agent both arms, N runs each
    if args.sandbox != "docker":
        report.note("Reruns ran without Docker: the agent had full access to the host.")
    agent = agent_by_name(args.rerun_agent)
    print(f"rerun: {agent.name}, {args.runs} runs per arm, {args.workers} at a time…")
    rr = Reruns(rp, agent, workers=args.workers, runlog=runlog)
    a_all, b_all = rr.run(rc, lesson, args.runs)
    if rr.blocked:
        print(f"rerun: blocked agent network access to {', '.join(rr.blocked)}")
        report.note(f"Reruns: the agent tried to reach {', '.join(rr.blocked)}; blocked (only the model API is allowed).")
    for r in a_all + b_all:
        if r.outcome != "PASS":
            report.note(f"Rerun arm {r.arm}: {r.outcome.lower()}, {r.detail}")
    # OTHER (agent never really ran: quota, auth, crash) is never evidence
    a = [r for r in a_all if r.outcome != "OTHER"]
    b = [r for r in b_all if r.outcome != "OTHER"]
    if not a or not b:
        why = next(r.detail for r in a_all + b_all if r.outcome == "OTHER")
        print(f"rerun: INCONCLUSIVE, no valid runs in {'both arms' if not a and not b else 'one arm'} ({why})")
        report["rerun"] = {"agent": agent.name, "a": [r.outcome for r in a_all], "b": [r.outcome for r in b_all],
                           "p": None, "verdict": "INCONCLUSIVE", "saved_to_brain": False}
        return 1
    ap, bp = sum(r.outcome == "PASS" for r in a), sum(r.outcome == "PASS" for r in b)
    verdict, p = decide(ap, len(a), bp, len(b))
    dropped = len(a_all) + len(b_all) - len(a) - len(b)
    saved = False
    if verdict == "VALIDATED" and source != "brain":
        try:
            entire.brain_save({"text": lesson, "files": [rc.step.file], "failing_output": rc.failing_output,
                               "verdict": verdict, "p": p, "agent": agent.name, "session_id": rc.step.session_id,
                               "step_id": rc.step.id, "runs": {"a": [r.outcome for r in a_all],
                                                               "b": [r.outcome for r in b_all]}})
            saved = True
        except Exception as e:  # noqa: BLE001
            print(f"rerun: Brain save failed ({e})")
    print(f"rerun: without lesson {ap}/{len(a)} pass · with lesson {bp}/{len(b)} pass · p = {p:.3f} → {verdict}"
          + (", saved to Brain" if saved else "") + (f" ({dropped} runs dropped as OTHER)" if dropped else ""))
    report["rerun"] = {"agent": agent.name, "a": [r.outcome for r in a_all], "b": [r.outcome for r in b_all],
                       "p": p, "verdict": verdict, "saved_to_brain": saved,
                       "tokens": sum(r.tokens or 0 for r in a_all + b_all) or None}
    return 0


def cmd_root_cause(args) -> int:
    """Dev entry for B: run stages 7-8 from a hard-coded RootCause JSON (the H16 gate)."""
    from .replay import Replayer
    from .tiger import RunLog

    fx = json.loads(Path(args.root_cause).read_text(encoding="utf-8"))
    if "root_cause" not in fx:  # the bare root_cause.json a real analysis writes (needs --test)
        fx = {"root_cause": fx}
    rc = RootCause.from_dict(fx["root_cause"])
    test = args.test or fx.get("test")
    if not test:
        print("--root-cause: this file has no test command, pass --test")
        return 2
    head = rc.head_state.base_commit
    report = Report(repo=Path.cwd().name, test=test, good=fx.get("good"), head=head, line=fx.get("line"))
    report.note(f"Stages 7–8 only, from the hard-coded root cause in {args.root_cause}.")
    report["root_cause"] = {"step_id": rc.step.id, "failing_output": rc.failing_output}
    report["session"] = {"id": rc.step.session_id, "agent": rc.step.agent, "prompt": rc.step.prompt,
                         "steps": [vars(s) for s in rc.edits_before + [rc.step]]}
    sandbox = _make_sandbox(args, head)
    if sandbox is None:
        return 2
    runlog = RunLog(label=f"{Path.cwd().name}: {test}")
    rp = Replayer(".", rc.edits_before + [rc.step], test, sandbox=sandbox, runlog=runlog, workers=args.workers)
    try:
        code = stages_7_8(rc, rp, args, report, runlog)
    finally:
        runlog.close()
    return _finish(report, args, code)


def cmd_analyze(args) -> int:
    from .replay import Replayer, is_test_file, vote
    from .tiger import RunLog

    head = _git("rev-parse", "HEAD")
    good = _git("rev-parse", args.good)
    report = Report(repo=Path.cwd().name, test=args.test, good=good, head=head, line=args.line)
    runlog = RunLog(label=f"{Path.cwd().name}: {args.test}")

    def run(rp, stage, state, n):
        rp.stage = stage
        res = rp.run(state, n)
        report.log_runs(stage, state_label(state), res)
        return res

    def outcome(rp, stage, state, n):
        return vote([r.outcome for r in run(rp, stage, state, n)])

    # Stage 0 — precheck (tests pinned from HEAD)
    sandbox = _make_sandbox(args, head)
    if sandbox is None:
        return 2
    rp = Replayer(".", [], args.test, sandbox=sandbox, runlog=runlog, workers=args.workers)
    # 5 runs each, HEAD and good in parallel. With 3, a test that fails 35% of the time looks
    # stable about 4% of the time, and stable mode's single run per state then misreads it.
    N = PRECHECK_RUNS
    rp.stage = "precheck"
    head_res, good_res = rp.run_many([State(head, [], head), State(good, [], head)], N)
    report.log_runs("precheck", state_label(State(head, [], head)), head_res)
    report.log_runs("precheck", state_label(State(good, [], head)), good_res)
    g_pass = sum(r.outcome == PASS for r in good_res)
    h_fail = sum(r.outcome != PASS for r in head_res)
    mode = "stable" if h_fail == N else "flaky"
    report["precheck"] = {"head_fail": h_fail, "good_pass": g_pass, "n": N, "mode": mode}
    if g_pass != N:
        print(f"precheck: good commit passes {g_pass}/{N} → abort: the test fails without the agent's changes")
        report.note(f"Aborted: the good commit doesn't pass {N}/{N}, so the test fails without the agent's changes.")
        return _finish(report, args, 2)
    if h_fail == 0:
        print(f"precheck: HEAD passes {N}/{N} → nothing to explain")
        report.note(f"HEAD passes {N}/{N}, nothing to explain.")
        return _finish(report, args, 2)
    print(f"precheck: HEAD fails {h_fail}/{N}, good commit passes {N}/{N} → {mode}")
    target_sig = next(r.signature for r in head_res if r.outcome != PASS)
    report["precheck"]["signature"] = target_sig

    # Stage 1 — guilty commit (blame hint, then git bisect)
    rp = Replayer(".", [], args.test, target_sig=target_sig, sandbox=sandbox, runlog=runlog, workers=args.workers)
    n = 5 if mode == "flaky" else 1  # flaky: any target failure in 5 runs counts (replay.vote)
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
        # Tests are pinned to HEAD, so a commit that only touches test files runs exactly the same code
        # as its parent and can never be guilty. Leave those out (else a flaky pass blames them).
        def tests_only(c: str) -> bool:
            files = _git("diff-tree", "--no-commit-id", "--name-only", "-r", c).split()  # empty for merges
            return bool(files) and all(is_test_file(f) for f in files)

        commits = [c for c in _git("rev-list", "--reverse", f"{good}..{head}").split() if not tests_only(c)]
        if not commits:
            print("commit: every commit since --good only touches tests → nothing to blame")
            report.note("Every commit between --good and HEAD only changes test files, which are pinned to HEAD.")
            return _finish(report, args, 2)
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
    # one replayer for stages 2-8: it knows the session's edits, and its cache carries over to reruns
    rp2 = Replayer(".", steps, args.test, target_sig=target_sig, sandbox=sandbox, runlog=runlog, workers=args.workers)
    try:
        rc = find_root_cause(".", steps, fid, guilty, parent, head, rp2, opts, report, runlog=runlog)
        print(f"test runs used: {rp.runs + rp2.runs}")
        if rc is None:
            return _finish(report, args, 1)
        rc_path = Path(args.report).with_name("root_cause.json")
        rc_path.parent.mkdir(parents=True, exist_ok=True)
        rc_path.write_text(json.dumps(asdict(rc), indent=2), encoding="utf-8")
        print(f"root cause handoff: {rc_path}")
        # Stages 7–8 (B): lesson, then counterfactual reruns
        code = stages_7_8(rc, rp2, args, report, runlog)
    finally:
        runlog.close()
    return _finish(report, args, code)


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
    p.add_argument("--check-entire", action="store_true", help="smoke-test the Entire connection and exit")
    p.add_argument("--report", default=".whyagent/report.html", help="where to write the HTML report (+ .json)")
    p.add_argument("--open", action="store_true", help="open the HTML report in a browser when done")
    p.add_argument("--render", metavar="JSON", help="render a saved report .json to HTML and exit")
    p.add_argument("--rerun-agent", default="gemini-cli", choices=["gemini-cli", "claude-code"],
                   help="agent for counterfactual reruns (default gemini-cli; claude-code needs ANTHROPIC_API_KEY)")
    p.add_argument("--workers", type=int, default=2, help="sandboxes in parallel, for test runs and reruns (default 2; 4+ on a Vultr VM)")
    p.add_argument("--chart", action="store_true", help="write the Tiger Data pruning chart (.whyagent/chart.html)")
    p.add_argument("--lesson", help="test this lesson in the reruns instead of writing one with Gemini (Stage 7)")
    p.add_argument("--root-cause", metavar="JSON", help="dev: run stages 7-8 from a hard-coded RootCause fixture")
    args = p.parse_args(argv)
    if args.check_entire:
        return cmd_check_entire(args)
    if args.render:
        return cmd_render(args)
    if args.chart:
        return cmd_chart(args)
    if args.root_cause:
        return cmd_root_cause(args)
    if not (args.test and args.good):
        p.error("--test and --good are required (or use --check-entire)")
    return cmd_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
