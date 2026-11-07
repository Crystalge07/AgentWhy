"""whyagent CLI: wires stages 0-8 and prints the report."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import entire
from .report import Report, state_label
from .types import PASS, TARGET_FAIL, RootCause, State


PRECHECK_RUNS = 5
FLAKY_ESTIMATE_RUNS = 15  # HEAD runs used to estimate a flaky test's failure rate
MISS = 0.05               # flaky mode: chance a buggy state passes every one of its runs


def rate_lower_bound(fails: int, n: int, z: float = 1.28) -> float:
    """One-sided 90% Wilson lower bound on a failure rate. Sizing from the point estimate would trust a
    lucky precheck: 6/15 fails reads as 40% for a bug that really fails 28%, and too few runs follow."""
    if n == 0:
        return 0.0
    p = fails / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return max(0.0, (centre - margin) / (1 + z * z / n))


def flaky_runs(fail_rate: float, miss: float = MISS, lo: int = 5, hi: int = 30) -> int:
    """Runs per state so a state with the bug fails at least once with probability >= 1 - miss.
    A bug failing 28% of runs needs 10 (5 runs miss it 19% of the time, per state)."""
    import math

    if fail_rate >= 1:
        return 1
    if fail_rate <= 0:
        return hi
    return max(lo, min(hi, math.ceil(math.log(miss) / math.log(1 - fail_rate))))


def _git(*a) -> str:
    return subprocess.run(["git", *a], capture_output=True, encoding="utf-8", errors="replace", check=True).stdout.strip()


def line_from_traceback(output: str, is_test=None) -> str | None:
    """The deepest non-test frame in pytest's failure output ("path.py:39: ApiError"), as file:line.
    PRD: --line is parsed from the traceback when omitted. None when the failure is in the test itself."""
    import re

    from .replay import is_test_file

    is_test = is_test or is_test_file
    hits = [(m.group(1).replace("\\", "/"), m.group(2))
            for m in re.finditer(r"^([\w./\\-]+\.py):(\d+): \S", output, flags=re.M)]
    hits = [h for h in hits if not is_test(h[0])]
    return f"{hits[-1][0]}:{hits[-1][1]}" if hits else None


def cmd_check_entire(_args) -> int:
    ok_all = True
    for name, ok, detail in entire.check_connection():
        ok_all &= ok
        print(f"{'✓' if ok else '✗'} {name:<26} {detail}")
    return 0 if ok_all else 1


def _finish(report: Report, args, code: int) -> int:
    path = report.write(args.report, tiger=not getattr(args, "live", False))  # --live never touches Tiger
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
    if not lesson and not os.environ.get("GEMINI_API_KEY"):
        print("lesson: skipped, GEMINI_API_KEY isn't set (pass --lesson TEXT to test a lesson without Gemini)")
        report.note("Lesson skipped: no GEMINI_API_KEY and no --lesson.")
        return 0 if args.no_rerun else 1
    if not lesson:
        try:
            lesson = make_lesson(rc)
        except Exception as e:
            msg = str(e).split(". {")[0][:200]
            print(f"lesson: Gemini failed ({type(e).__name__}: {msg})")
            report.note(f"Lesson generation failed: {msg}")
            return 0 if args.no_rerun else 1  # the root cause is proven either way; only reruns need a lesson
    hidden = "" if rc.step.reasoning else " (agent reasoning not recorded; built from trigger + diff)"
    print(f'lesson: "{lesson}"' + {"brain": " (from Brain)", "given": " (from --lesson)"}.get(source, "") + hidden)
    report["lesson"] = {"text": lesson, "source": source}
    if args.no_rerun:
        return 0

    # Stage 8 — same agent both arms, N runs each
    if args.sandbox != "docker":
        report.note("Reruns ran without Docker: the agent had full access to the host.")
    agent = agent_by_name(args.rerun_agent)
    missing = agent.missing_auth()
    if missing:
        print(f"rerun: skipped, {missing}")
        report.note(f"Reruns skipped: {missing}.")
        report["rerun"] = {"agent": agent.name, "a": [], "b": [], "p": None, "verdict": "INCONCLUSIVE",
                           "saved_to_brain": False}
        return 1
    target_runs = max(1, getattr(args, "target_runs", None) or 1)
    print(f"rerun: {agent.name}, {args.runs} runs per arm, {args.workers} at a time"
          + (f", target test {target_runs}× per rerun (flaky)" if target_runs > 1 else "") + "…")
    if args.runs < 4:
        print(f"rerun: note: with {args.runs} runs per arm the Fisher test can't reach p < 0.05 (VALIDATED needs 4+)")
    rr = Reruns(rp, agent, workers=args.workers, runlog=runlog, target_runs=target_runs)
    a_all, b_all = rr.run(rc, lesson, args.runs)
    if rr.excluded:
        print(f"rerun: {len(rr.excluded)} test(s) added by the session aren't required of reruns")
        report.note("Reruns weren't required to pass these tests, which pass at the guilty commit but not before "
                    "the session (written for code the original agent invented): " + ", ".join(rr.excluded[:8])
                    + (" …" if len(rr.excluded) > 8 else ""))
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


def stage_live(rc: RootCause, rp, args, report: Report, checkpoint: str | None) -> int:
    """--live: diagnose → rule → approve → rewind → rerun → verify, instead of stages 7–8. Never touches Brain."""
    from .live import LiveLoopError, LiveOptions, run_live
    from .model import ModelError, make_model

    try:
        model = make_model(args.model, args.rerun_agent, local=args.sandbox == "local")
        missing = model.missing_auth()
        if missing:
            raise LiveLoopError("diagnose", missing)
    except ModelError as e:
        print(f"\n✗ live loop failed at diagnose: {e}")
        report.note(f"Live loop failed at diagnose: {e}")
        return 2
    except LiveLoopError as e:
        print(f"\n✗ live loop failed at {e.stage}: {e.why}")
        report.note(f"Live loop failed at {e.stage}: {e.why}")
        return 2
    opts = LiveOptions(test=args.test, instructions=args.instructions, runs=args.runs, yes=args.yes,
                       workers=args.workers, target_runs=max(1, args.target_runs or 1), checkpoint=checkpoint)
    print(f"\nlive loop: {' → '.join(['diagnose', 'rule', 'approve', 'rewind', 'rerun', 'verify'])}")
    try:
        report["live"] = run_live(rc, rp, model, opts)
    except LiveLoopError as e:
        print(f"\n✗ live loop failed at {e.stage}: {e.why}")
        report["live"] = {"failed_stage": e.stage, "error": e.why}
        report.note(f"Live loop failed at {e.stage}: {e.why}")
        return 2
    return 0 if report["live"]["after"]["outcome"] == "PASS" else 1


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
    report = Report(repo=Path.cwd().name, test=test, good=fx.get("good"), head=head, line=fx.get("line"),
                    command=getattr(args, "command", None))
    report.note(f"Stages 7–8 only, from the hard-coded root cause in {args.root_cause}.")
    report["root_cause"] = {"step_id": rc.step.id, "failing_output": rc.failing_output}
    report["session"] = {"id": rc.step.session_id, "agent": rc.step.agent, "prompt": rc.step.prompt,
                         "steps": [vars(s) for s in rc.edits_before + [rc.step]]}
    sandbox = _make_sandbox(args, head)
    if sandbox is None:
        return 2
    runlog = RunLog(label=f"{Path.cwd().name}: {test}", url="" if args.live else None)  # --live: no Tiger
    report["meta"]["analysis_id"] = runlog.analysis_id
    rp = Replayer(".", rc.edits_before + [rc.step], test, sandbox=sandbox, runlog=runlog, workers=args.workers)
    try:
        if args.live:
            args.test = test
            code = stage_live(rc, rp, args, report, _saved_checkpoint(Path(args.root_cause)))
        else:
            code = stages_7_8(rc, rp, args, report, runlog)
    except Exception as e:  # noqa: BLE001 — write the report anyway
        code = _crashed(report, e)
    finally:
        runlog.close()
    return _finish(report, args, code)


def _saved_checkpoint(rc_path: Path) -> str | None:
    """The Entire checkpoint id from the report.json saved beside a root_cause.json, if there is one."""
    try:
        return json.loads(rc_path.with_name("report.json").read_text(encoding="utf-8")).get("commit", {}).get("checkpoint")
    except (OSError, ValueError, AttributeError):
        return None


def _crashed(report: Report, e: Exception) -> int:
    """An unexpected error mid-analysis: say so, and let the caller still write the report."""
    msg = (str(e).strip().splitlines() or [""])[0][:300]
    print(f"error: {type(e).__name__}: {msg} (the report keeps everything found so far)")
    if os.environ.get("WHYAGENT_DEBUG"):
        import traceback
        traceback.print_exc()
    report.note(f"Analysis stopped by an error: {type(e).__name__}: {msg}")
    return 2


def cmd_analyze(args) -> int:
    from .tiger import RunLog

    head = _git("rev-parse", "HEAD")
    good = _git("rev-parse", args.good)
    report = Report(repo=Path.cwd().name, test=args.test, good=good, head=head, line=args.line,
                    command=getattr(args, "command", None))
    runlog = RunLog(label=f"{Path.cwd().name}: {args.test}", url="" if args.live else None)  # --live: no Tiger
    report["meta"]["analysis_id"] = runlog.analysis_id  # joins this report to its Tiger rows
    try:
        return _analyze(args, head, good, report, runlog)
    except Exception as e:  # noqa: BLE001 — an error in any stage still writes the report
        return _finish(report, args, _crashed(report, e))
    finally:
        runlog.close()


# pytest: 4 = usage error (e.g. a test id that doesn't exist), 5 = no tests collected
_NOT_A_TEST_RUN = {4: "pytest usage error: is the test id right?", 5: "pytest collected no tests: is the test id right?"}


def _analyze(args, head: str, good: str, report: Report, runlog) -> int:
    from .bisect import prefix_bisect
    from .replay import Replayer, is_test_file, vote

    def run(rp, stage, state, n):
        rp.stage = stage
        res = rp.run(state, n, until_fail=n > 1)
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
    bad_cmd = next((r.exit_code for r in head_res + good_res if r.exit_code in _NOT_A_TEST_RUN), None) \
        if "pytest" in args.test else None
    if bad_cmd is not None:
        print(f"precheck: {_NOT_A_TEST_RUN[bad_cmd]} (exit {bad_cmd}) → abort")
        report.note(f"Aborted: {_NOT_A_TEST_RUN[bad_cmd]} (pytest exit {bad_cmd}).")
        return _finish(report, args, 2)
    g_pass = sum(r.outcome == PASS for r in good_res)
    h_fail = sum(r.outcome != PASS for r in head_res)
    mode = "stable" if h_fail == N else "flaky"
    report["precheck"] = {"head_fail": h_fail, "good_pass": g_pass, "n": N, "mode": mode}
    if g_pass != N:
        print(f"precheck: good commit passes {g_pass}/{N} → abort: the test fails without the agent's changes")
        report.note(f"Aborted: the good commit doesn't pass {N}/{N}, so the test fails without the agent's changes.")
        return _finish(report, args, 2)
    if h_fail == 0:
        # The user says this test fails, and a flaky bug can pass 5 in a row (the recorded ~28% cache bug
        # does ~19% of the time). Look harder before giving up; these runs are reused by the flaky estimate.
        rp.stage = "precheck"
        head_res = rp.run(State(head, [], head), FLAKY_ESTIMATE_RUNS)
        report.log_runs("precheck", state_label(State(head, [], head)), head_res[N:])
        h_fail = sum(r.outcome != PASS for r in head_res)
        if h_fail == 0:
            print(f"precheck: HEAD passes {len(head_res)}/{len(head_res)} → nothing to explain")
            report.note(f"HEAD passes {len(head_res)}/{len(head_res)}, nothing to explain.")
            return _finish(report, args, 2)
        mode = "flaky"
        report["precheck"].update(head_fail=h_fail, n=len(head_res), mode=mode)
    print(f"precheck: HEAD fails {h_fail}/{len(head_res)}, good commit passes {N}/{N} → {mode}")
    target_sig = next(r.signature for r in head_res if r.outcome != PASS)
    report["precheck"]["signature"] = target_sig
    n = 1
    if mode == "flaky":
        # Size runs per state from the failure rate, so a state with the bug reads as failing 95% of the time
        rp.stage = "precheck"
        more = rp.run(State(head, [], head), FLAKY_ESTIMATE_RUNS)
        report.log_runs("precheck", state_label(State(head, [], head)), more[N:])
        fails = sum(r.outcome != PASS for r in more)
        rate, low = fails / len(more), rate_lower_bound(fails, len(more))
        n = flaky_runs(low)
        good_more = rp.run(State(good, [], head), n)  # the good commit must pass as many runs as any state
        report.log_runs("precheck", state_label(State(good, [], head)), good_more[N:])
        if any(r.outcome != PASS for r in good_more):
            print(f"precheck: good commit fails {sum(r.outcome != PASS for r in good_more)}/{n} → abort")
            report.note("Aborted: the good commit fails some runs too, so the test fails without the agent's changes.")
            return _finish(report, args, 2)
        report["precheck"].update(fail_rate=round(rate, 2), fail_rate_low=round(low, 2), runs_per_state=n)
        print(f"precheck: HEAD fails {fails}/{len(more)} runs ({round(rate * 100)}%, at least {round(low * 100)}% "
              f"with 90% confidence) → {n} runs per state, so a state with the bug shows it ≥95% of the time")
    if not args.line:
        found = line_from_traceback(next(r.output for r in head_res if r.outcome != PASS))
        if found and subprocess.run(["git", "cat-file", "-e", f"HEAD:{found.rsplit(':', 1)[0]}"],
                                    capture_output=True).returncode == 0:
            args.line = report["meta"]["line"] = found
            print(f"blame hint: {found}, from the traceback (pass --line to override)")

    # Stage 1 — guilty commit (blame hint, then git bisect)
    rp = Replayer(".", [], args.test, target_sig=target_sig, sandbox=sandbox, runlog=runlog, workers=args.workers)
    # flaky: n runs per state from the precheck; any target failure counts (replay.vote)
    guilty = blamed = None
    if args.line:
        path, ln = args.line.rsplit(":", 1)
        c = _git("blame", "-L", f"{ln},{ln}", "--porcelain", "HEAD", "--", path).split()[0]
        blamed = c
        in_range = subprocess.run(["git", "merge-base", "--is-ancestor", c, good], capture_output=True).returncode != 0
        if not in_range:  # written at or before --good, where the test passes: it can't be the guilty commit
            print(f"commit: blame hint {c[:7]} is older than --good, bisecting")
        parent = _git("rev-parse", f"{c}^") if in_range else None
        if (in_range and outcome(rp, "commit", State(parent, [], head), n) == PASS
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
        # Same search as the edit-level bisect: a commit that crashes or fails differently (OTHER) is
        # never evidence, so the nearest other commit is tried instead.
        k = prefix_bisect(commits, lambda prefix: outcome(rp, "commit", State(prefix[-1], [], head), n))
        if k is None:
            print("commit: bisect inconclusive (the commits around the boundary crash or fail differently)")
            report.note("Commit bisect inconclusive: every commit around the boundary was OTHER.")
            return _finish(report, args, 2)
        guilty = commits[k - 1]
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
    args.target_runs = args.target_runs or n  # flaky: a rerun's fix must survive as many runs as any state
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
        if args.live:  # diagnose → rule → approve → rewind → rerun → verify, instead of stages 7–8
            code = stage_live(rc, rp2, args, report, cp)
        else:  # Stages 7–8 (B): lesson, then counterfactual reruns
            code = stages_7_8(rc, rp2, args, report, runlog)
        if args.trail:
            from .trail import post_finding
            try:
                out = post_finding(rc, report, client_id=f"whyagent-{runlog.analysis_id}")
                print(f"trail: posted the root cause as a finding\n{out.strip()}")
                report["trail"] = {"posted": True, "output": out.strip()}
            except Exception as e:  # noqa: BLE001 — the analysis already succeeded
                print(f"trail: couldn't post the finding ({str(e).splitlines()[0][:200]})")
                report.note(f"Couldn't post the trail finding: {e}")
    finally:
        runlog.close()
    return _finish(report, args, code)


def main(argv=None) -> int:
    # Windows consoles/pipes default to cp1252, which can't print the ✓/✗ marks.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    from dotenv import find_dotenv, load_dotenv

    # .env (gitignored): the analyzed repo's or a parent's first, then the one next to this install
    # (an editable install in a clone of whyagent). Neither overrides variables already set.
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv()
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
    p.add_argument("--rerun-agent", default="gemini-cli", choices=["gemini-cli", "claude-code", "stub"],
                   help="agent for counterfactual reruns (default gemini-cli; claude-code needs ANTHROPIC_API_KEY; "
                        "stub runs $WHYAGENT_STUB_CMD, no model, for rehearsing Stage 8)")
    p.add_argument("--target-runs", type=int, help="runs of the target test per rerun (default: the precheck's runs "
                                                    "per state, so flaky bugs need to stay fixed; 1 with --root-cause)")
    p.add_argument("--workers", type=int, default=2, help="sandboxes in parallel, for test runs and reruns (default 2; 4+ on a Vultr VM)")
    p.add_argument("--chart", action="store_true", help="write the Tiger Data pruning chart (.whyagent/chart.html)")
    p.add_argument("--trail", action="store_true",
                   help="post the root cause as a finding on this branch's Entire trail, with the revert as a suggested patch")
    p.add_argument("--lesson", help="test this lesson in the reruns instead of writing one with Gemini (Stage 7)")
    p.add_argument("--root-cause", metavar="JSON", help="dev: run stages 7-8 from a hard-coded RootCause fixture")
    p.add_argument("--live", action="store_true",
                   help="after the root cause: write a rule from the reruns, approve it, add it to --instructions, "
                        "rewind, rerun the agent with it and verify (replaces stages 7-8; never touches Brain or Tiger)")
    p.add_argument("--yes", action="store_true", help="--live: approve the rule without asking")
    p.add_argument("--instructions", default="AGENTS.md",
                   help="--live: the agent instructions file the rule is added to (default AGENTS.md)")
    p.add_argument("--model", choices=["gemini", "mock"],
                   help="--live: model for the rule and the rerun agent (default $WHYAGENT_MODEL, else gemini; "
                        "mock = canned responses for the seeded demo repo)")
    args = p.parse_args(argv)
    import shlex
    args.command = "whyagent " + shlex.join(sys.argv[1:] if argv is None else argv)  # shown on the report
    if args.live and (args.trail or args.lesson or args.no_rerun):
        p.error("--live can't be combined with --trail, --lesson or --no-rerun")
    if (args.yes or args.model) and not args.live:
        p.error("--yes and --model only apply to --live")
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
