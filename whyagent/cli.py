"""whyagent CLI: wires stages 0-8 and prints the report."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import entire, events
from .report import Report, state_label
from .report import error_line as report_error_line
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


def _at_head(path: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"HEAD:{path}"], capture_output=True).returncode == 0


def line_from_traceback(output: str, is_test=None, exists=_at_head) -> str | None:
    """The deepest repo frame in pytest's failure output ("path.py:39: ApiError") that isn't a test, as
    file:line. PRD: --line is parsed from the traceback when omitted. Frames outside the repo (a library
    in site-packages raising) are skipped, so the hint lands on the repo code that called it. None when
    the failure is in the test itself."""
    import re

    from .replay import is_test_file

    is_test = is_test or is_test_file
    hits = [(m.group(1).replace("\\", "/"), m.group(2))
            for m in re.finditer(r"^([\w./\\-]+\.py):(\d+): \S", output, flags=re.M)]
    hits = [h for h in hits if not is_test(h[0]) and exists(h[0])]
    return f"{hits[-1][0]}:{hits[-1][1]}" if hits else None


def cmd_check_entire(_args) -> int:
    ok_all = True
    for name, ok, detail in entire.check_connection():
        ok_all &= ok
        print(f"{'✓' if ok else '✗'} {name:<26} {detail}")
    return 0 if ok_all else 1


def _history(args) -> Path:
    """Every analysis keeps its own copy here, for `whyagent --serve`'s results list."""
    return Path(args.report).parent / "history"


def _start_events(args, report: Report, analysis_id: str) -> None:
    from . import events

    m = report["meta"]
    events.start(_history(args) / analysis_id, id=analysis_id, repo=m.get("repo"), test=m.get("test"),
                 good=m.get("good"), head=m.get("head"), line=m.get("line"), command=m.get("command"),
                 workers=getattr(args, "workers", None), sandbox=getattr(args, "sandbox", None))


def _finish(report: Report, args, code: int) -> int:
    import shutil

    from . import events

    path = report.write(args.report, tiger=not getattr(args, "live", False))  # --live never touches Tiger
    aid = report["meta"].get("analysis_id")
    if aid:
        hist = _history(args) / aid
        hist.mkdir(parents=True, exist_ok=True)
        for f in (path, path.with_suffix(".json")):
            if f.resolve() != (hist / f.name).resolve():
                shutil.copyfile(f, hist / f.name)
    print(f"report: {path}")
    events.emit("done", code=code)  # last: the live view stops listening after it
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
    """Stage 7 (lesson) and Stage 8 (counterfactual reruns), then the fix PR (on unless --no-pr), however
    stages 7-8 ended: with the rerun's fix and the rule when the lesson VALIDATED, else with the revert."""
    out: dict = {}
    code = _lesson_and_reruns(rc, rp, args, report, runlog, out)
    if getattr(args, "pr", False):
        stage_pr(rc, rp, out.get("rr"), out.get("b_all") or [], out.get("lesson") or "", out.get("verdict"),
                 args, report)
    return code


def _lesson_and_reruns(rc: RootCause, rp, args, report: Report, runlog, out: dict) -> int:
    """Stages 7-8. rp must know rc's steps. Fills `out` with lesson, rr, b_all and verdict as they become
    known, for the PR step."""
    from .model import model_name
    from .rerun import Reruns, agent_by_name, decide, fisher_one_sided
    from .rerun.lesson import GENERATORS, generator_missing

    # Stage 7 — Brain first, then the --model writer (Gemini or Claude)
    writer = model_name(getattr(args, "model", None))
    writer = writer if writer in GENERATORS else "gemini"
    label = {"gemini": "Gemini", "claude": "Claude"}[writer]
    lesson, source = None, writer
    events.stage("lesson")
    try:
        hit = entire.brain_lookup([rc.step.file], rc.failing_output)  # a validated lesson record, or None
        events.emit("lookup", found=bool(hit), text=hit["text"] if hit else None)
        if hit:
            lesson, source = hit["text"], "brain"
    except Exception as e:  # noqa: BLE001
        events.emit("lookup", found=False, error=str(e)[:200])
        print(f"lesson: Brain lookup failed ({e}), writing a new one")
    if getattr(args, "lesson", None):
        lesson, source = args.lesson.strip(), "given"  # --lesson: test a hand-written lesson, skip the model
    # --model gemini (default) falls back to Claude on its own; an explicit --model claude needs Claude set up
    missing = None if lesson or writer == "gemini" else generator_missing(writer)
    if missing:
        print(f"lesson: skipped, {missing} (pass --lesson TEXT to test a lesson without {label})")
        report.note(f"Lesson skipped: {missing} and no --lesson.")
        return 0 if args.no_rerun else 1
    if not lesson:
        try:
            lesson, source = _new_lesson(rc, writer)
        except Exception as e:  # noqa: BLE001
            msg = str(e).split(". {")[0][:300]
            print(f"lesson: {'no provider could write it' if writer == 'gemini' else label + ' failed'} ({msg})")
            report.note(f"Lesson generation failed: {msg}")
            return 0 if args.no_rerun else 1  # the root cause is proven either way; only reruns need a lesson
    hidden = "" if rc.step.reasoning else " (agent reasoning not recorded; built from trigger + diff)"
    print(f'lesson: "{lesson}"' + {"brain": " (from Brain)", "given": " (from --lesson)",
                                   "claude": " (written by Claude)", "gemini": " (written by Gemini)"}.get(source, "")
          + hidden)
    report["lesson"] = {"text": lesson, "source": source}
    out["lesson"] = lesson
    events.stage("lesson", "done", text=lesson, source=source)
    if args.no_rerun:
        events.stage("rerun", "skip", why="--no-rerun")
        return 0
    if not (rc.step.prompt or "").strip():
        why = "the session's task prompt wasn't recorded, so a rerun would have no task to redo"
        print(f"rerun: skipped, {why}")
        report.note(f"Reruns skipped: {why}.")
        events.stage("rerun", "skip", why=why)
        return 0

    # Stage 8 — same agent both arms, N runs each
    if args.sandbox != "docker":
        report.note("Reruns ran without Docker: the agent had full access to the host.")
    from . import providers

    agent_name, fell_back = providers.pick_rerun_agent(args.rerun_agent)  # auto: Gemini CLI, else Claude Code
    if fell_back:
        print(f"rerun: {fell_back} → Claude Code reruns")
    agent = agent_by_name(agent_name)
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
    # Lesson retries test several lessons against the same no-lesson arm, so the 5% false-VALIDATED rate is
    # split across them (Bonferroni). A lesson the user passed is tested as given, never replaced.
    max_attempts = 1 if source == "given" else max(1, getattr(args, "lesson_attempts", None) or 1)
    alpha = 0.05 / max_attempts
    if fisher_one_sided(0, args.runs, args.runs, args.runs) >= alpha:
        need = next(n for n in range(1, 100) if fisher_one_sided(0, n, n, n) < alpha)
        print(f"rerun: note: with {args.runs} runs per arm the Fisher test can't reach p < {alpha:.3g}"
              f" (VALIDATED needs {need}+{f' with {max_attempts} lesson attempts' if max_attempts > 1 else ''})")
    events.stage("rerun", agent=agent.name, runs=args.runs, lesson=lesson, attempt=1)
    rr = Reruns(rp, agent, workers=args.workers, runlog=runlog, target_runs=target_runs)
    rr.capture_diff = bool(getattr(args, "pr", False))  # the PR builds its fix from a passing rerun's diff
    out["rr"] = rr
    a_all, b_all = rr.run(rc, lesson, args.runs)
    if (args.rerun_agent == "auto" and agent.name == "gemini-cli" and rr._abort
            and providers.claude_configured()):
        # Gemini CLI hit quota/auth mid-run. Both arms must use the same agent, so redo all of Stage 8.
        providers.mark_gemini_down(rr._abort)
        print(f"rerun: Gemini CLI failed ({rr._abort[:120]}) → rerunning both arms with Claude Code")
        agent = agent_by_name("claude-code")
        events.stage("rerun", agent=agent.name, runs=args.runs, lesson=lesson, attempt=1, restart=True)
        rr = Reruns(rp, agent, workers=args.workers, runlog=runlog, target_runs=target_runs)
        rr.capture_diff = bool(getattr(args, "pr", False))
        out["rr"] = rr
        a_all, b_all = rr.run(rc, lesson, args.runs)
    if rr.excluded:
        print(f"rerun: {len(rr.excluded)} test(s) added by the session aren't required of reruns")
        report.note("Reruns weren't required to pass these tests, which pass at the guilty commit but not before "
                    "the session (written for code the original agent invented): " + ", ".join(rr.excluded[:8])
                    + (" …" if len(rr.excluded) > 8 else ""))
    # OTHER (agent never really ran: quota, auth, crash) is never evidence
    a = [r for r in a_all if r.outcome != "OTHER"]
    for r in a_all:
        if r.outcome != "PASS":
            report.note(f"Rerun arm A: {r.outcome.lower()}, {r.detail}")
    ap = sum(r.outcome == "PASS" for r in a)
    attempts: list[dict] = []  # every lesson tested, in order; the ones that failed steer the next one
    tested: list[dict] = []  # full results per attempt, to report the best one if none validates
    attempt = 1
    while True:
        tag = f" (lesson {attempt})" if attempt > 1 else ""
        for r in b_all:
            if r.outcome != "PASS":
                report.note(f"Rerun arm B{tag}: {r.outcome.lower()}, {r.detail}")
        b = [r for r in b_all if r.outcome != "OTHER"]
        out["b_all"] = b_all
        if (not a or not b) and attempts:  # a retry's lesson arm never ran; keep the lessons already tested
            why = next(r.detail for r in b_all if r.outcome == "OTHER")
            print(f"rerun{tag}: INCONCLUSIVE, no valid with-lesson runs ({why}); keeping the earlier lessons")
            attempts.append({"text": lesson, "source": source, "verdict": "INCONCLUSIVE", "p": None,
                             "b": [r.outcome for r in b_all]})
            report["rerun"]["attempts"] = attempts
            events.stage("rerun", "error", verdict="INCONCLUSIVE", why=why, attempt=attempt)
            break
        if not a or not b:
            out["verdict"] = "INCONCLUSIVE"
            why = next(r.detail for r in a_all + b_all if r.outcome == "OTHER")
            print(f"rerun: INCONCLUSIVE, no valid runs in {'both arms' if not a and not b else 'one arm'} ({why})")
            attempts.append({"text": lesson, "source": source, "verdict": "INCONCLUSIVE", "p": None,
                             "b": [r.outcome for r in b_all]})
            report["rerun"] = {"agent": agent.name, "a": [r.outcome for r in a_all], "b": [r.outcome for r in b_all],
                               "p": None, "verdict": "INCONCLUSIVE", "saved_to_brain": False, "attempts": attempts}
            events.stage("rerun", "error", verdict="INCONCLUSIVE", why=why, attempt=attempt)
            _note_blocked(rr, report)
            return 1
        bp = sum(r.outcome == "PASS" for r in b)
        verdict, p = decide(ap, len(a), bp, len(b), alpha=alpha)
        out["verdict"] = verdict
        dropped = len(a_all) + len(b_all) - len(a) - len(b)
        saved = False
        if verdict == "VALIDATED" and source != "brain":
            try:
                entire.brain_save({"text": lesson, "files": [rc.step.file], "failing_output": rc.failing_output,
                                   "repo": entire.repo_name(rp.repo),
                                   "verdict": verdict, "p": p, "agent": agent.name, "session_id": rc.step.session_id,
                                   "step_id": rc.step.id, "runs": {"a": [r.outcome for r in a_all],
                                                                   "b": [r.outcome for r in b_all]}})
                saved = True
            except Exception as e:  # noqa: BLE001
                print(f"rerun: Brain save failed ({e})")
        print(f"rerun{tag}: without lesson {ap}/{len(a)} pass · with lesson {bp}/{len(b)} pass · p = {p:.3f} → "
              f"{verdict}" + (", saved to Brain" if saved else "")
              + (f" ({dropped} runs dropped as OTHER)" if dropped else ""))
        attempts.append({"text": lesson, "source": source, "verdict": verdict, "p": p,
                         "b": [r.outcome for r in b_all]})
        report["lesson"] = {"text": lesson, "source": source}
        report["rerun"] = {"agent": agent.name, "a": [r.outcome for r in a_all], "b": [r.outcome for r in b_all],
                           "p": p, "alpha": alpha, "verdict": verdict, "saved_to_brain": saved, "attempts": attempts,
                           "tokens": sum(r.tokens or 0 for r in a_all + b_all) or None}
        tested.append({"p": p, "lesson": dict(report["lesson"]), "rerun": dict(report["rerun"]), "b_all": b_all,
                       "verdict": verdict})
        events.stage("rerun", "done", verdict=verdict, p=p, a=f"{ap}/{len(a)}", b=f"{bp}/{len(b)}", attempt=attempt)
        # VALIDATED: done. NOT_NEEDED: agents without any lesson already pass, so no lesson can beat that.
        if verdict in ("VALIDATED", "NOT_NEEDED") or attempt >= max_attempts:
            break
        # INEFFECTIVE / PROMISING: the guilty edit is still a mistake agents make; write a different lesson,
        # told what was already tried and what still went wrong, and test it against the same no-lesson arm.
        attempt += 1
        tried = [{"text": t["text"], "verdict": t["verdict"], "a": [ap, len(a)],
                  "b": [sum(o == "PASS" for o in t["b"]), sum(o != "OTHER" for o in t["b"])],
                  "failures": t.get("failures")} for t in attempts]
        tried[-1]["failures"] = attempts[-1]["failures"] = [r.detail for r in b if r.outcome != "PASS"]
        print(f"rerun: lesson {verdict.lower()}, writing a different one (attempt {attempt} of {max_attempts})…")
        events.stage("lesson", attempt=attempt, previous=attempts[-1]["text"], previous_verdict=verdict)
        try:
            lesson, source = _new_lesson(rc, writer, tried)
        except Exception as e:  # noqa: BLE001
            msg = str(e).split(". {")[0][:300]
            print(f"lesson: couldn't write another one ({msg}); keeping the last result")
            report.note(f"Lesson attempt {attempt} not written: {msg}")
            events.stage("lesson", "error", why=msg, attempt=attempt)
            break
        print(f'lesson {attempt}: "{lesson}"')
        out["lesson"] = lesson
        report["lesson"] = {"text": lesson, "source": source}
        events.stage("lesson", "done", text=lesson, source=source, attempt=attempt)
        events.stage("rerun", agent=agent.name, runs=args.runs, lesson=lesson, attempt=attempt, keep_a=True)
        _, b_all = rr.run(rc, lesson, args.runs, arm_a=False, job0=args.runs * (attempt + 1))
    if len(attempts) > 1 and tested and tested[-1]["verdict"] != "VALIDATED":
        # none validated: report the lesson that came closest (lowest p), not just the last one tried
        best = min(tested, key=lambda x: x["p"])
        report["lesson"] = best["lesson"]
        report["rerun"] = {**best["rerun"], "attempts": attempts}
        out["lesson"], out["b_all"], out["verdict"] = best["lesson"]["text"], best["b_all"], best["verdict"]
        if best["lesson"]["text"] != attempts[-1]["text"]:
            print(f'rerun: no lesson validated; reporting the closest one (p = {best["p"]:.3f}): '
                  f'"{best["lesson"]["text"]}"')
    if len(attempts) > 1:
        report.note(f"Each lesson needed p < {alpha:.3g} (0.05 split across {max_attempts} attempts). "
                    "Lessons tested: " + " · ".join(f'#{i} {t["verdict"]}: "{t["text"]}"'
                                                    for i, t in enumerate(attempts, 1)))
    _note_blocked(rr, report)
    return 0


def _new_lesson(rc: RootCause, writer: str, tried: list[dict] | None = None) -> tuple[str, str]:
    """(lesson, source) from the --model writer; Gemini falls back to Claude. `tried`: lessons that failed."""
    from .rerun import make_lesson
    from .rerun.lesson import GENERATORS, write_lesson

    if writer != "gemini":
        return make_lesson(rc, GENERATORS[writer], tried=tried), writer
    lesson, source, fell = write_lesson(rc, tried=tried)  # Gemini first, Claude if Gemini can't
    for t in fell:
        print(f"lesson: {t} → writing it with Claude")
    return lesson, source


def _note_blocked(rr, report: Report) -> None:
    if rr.blocked:
        print(f"rerun: blocked agent network access to {', '.join(rr.blocked)}")
        report.note(f"Reruns: the agent tried to reach {', '.join(rr.blocked)}; blocked (only the model API is allowed).")


def stage_pr(rc: RootCause, rp, rr, b_all, lesson: str, verdict: str | None, args, report: Report) -> None:
    """Open a GitHub PR with a fix verified at HEAD, after every analysis that found a root cause (--no-pr
    turns it off). A VALIDATED lesson brings the passing rerun's fix and the rule in --instructions; otherwise
    the PR reverts the guilty edit and adds no rule. Never fails the analysis."""
    from types import SimpleNamespace

    from .fix_pr import FixError, make_pr

    validated = verdict == "VALIDATED"
    rule = lesson if validated else ""
    if rr is None:  # no reruns ran (--no-rerun, no lesson, no agent auth): verify the revert with the test alone
        rr = SimpleNamespace(target_runs=max(1, getattr(args, "target_runs", None) or 1), _baseline=None,
                             suite_cmd=None)
    if validated:
        print(f"pr: finding a fix that passes at HEAD, rule → {args.instructions}…")
    else:
        print(f"pr: lesson {verdict or 'not tested'}, so no rule; opening a PR that reverts the guilty edit…")
    try:
        pr = make_pr(rp, rr, rc, b_all if validated else [], rule, report, instructions=args.instructions,
                     remote=args.pr_remote)
    except FixError as e:
        print(f"pr: not opened, {e}")
        report.note(f"PR not opened: {e}")
        report["pr"] = {"opened": False, "why": str(e)}
        return
    except Exception as e:  # noqa: BLE001 — the analysis already succeeded
        print(f"pr: not opened ({type(e).__name__}: {e})")
        report.note(f"PR not opened: {type(e).__name__}: {e}")
        report["pr"] = {"opened": False, "why": f"{type(e).__name__}: {e}"}
        return
    print(f"pr: opened {pr['url']} ({'rerun fix' if pr['fix'] == 'rerun' else 'revert'}, branch {pr['branch']})")
    if pr.get("warning"):
        print(f"pr: note: {pr['warning']}")
        report.note(f"PR: {pr['warning']}")
    report["pr"] = {"opened": True, "rule": bool(rule), **pr}


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


def cmd_brain() -> int:
    """--brain: every validated lesson, oldest first, with its evidence."""
    recs = entire._brain_records()
    where = "Tiger Data" if os.environ.get("TIGER_DATABASE_URL") else "TIGER_DATABASE_URL not set"
    print(f"{len(recs)} validated lesson(s) ({where})")
    for r in recs:
        runs = r.get("runs") or {}
        a, b = runs.get("a") or [], runs.get("b") or []
        p = r.get("p")
        print(f"\n[{(r.get('saved_at') or '?')[:16].replace('T', ' ')}] "
              + (f"{r['repo']}: " if r.get("repo") else "") + f"{', '.join(r.get('files') or [])}"
              + (f" · {r['error_type']}" if r.get("error_type") else "")
              + (f" · p = {p:.3f}" if isinstance(p, (int, float)) else "")
              + (f" · {r['agent']}" if r.get("agent") else "")
              + (f" · without {a.count('PASS')}/{len(a)}, with {b.count('PASS')}/{len(b)} pass" if a or b else ""))
        print(f"  {r.get('text')}")
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
    report = Report(repo=Path.cwd().name, test=test, good=fx.get("good"), head=head, line=fx.get("line"),
                    command=getattr(args, "command", None))
    report.note(f"Stages 7–8 only, from the hard-coded root cause in {args.root_cause}.")
    report["root_cause"] = {"step_id": rc.step.id, "failing_output": rc.failing_output}
    report["session"] = {"id": rc.step.session_id, "agent": rc.step.agent, "prompt": rc.step.prompt,
                         "steps": [vars(s) for s in rc.edits_before + [rc.step]]}
    sandbox = _make_sandbox(args, head)
    if sandbox is None:
        return 2
    runlog = RunLog(analysis_id=args.id, label=f"{Path.cwd().name}: {test}",
                    url="" if args.live else None)  # --live: no Tiger
    report["meta"]["analysis_id"] = runlog.analysis_id
    _start_events(args, report, runlog.analysis_id)
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
    events.emit("error", text=f"{type(e).__name__}: {msg}")
    return 2


def cmd_analyze(args) -> int:
    from .tiger import RunLog

    head = _git("rev-parse", "HEAD")
    good = _git("rev-parse", args.good)
    report = Report(repo=Path.cwd().name, test=args.test, good=good, head=head, line=args.line,
                    command=getattr(args, "command", None))
    runlog = RunLog(analysis_id=args.id, label=f"{Path.cwd().name}: {args.test}", workers=args.workers,
                    url="" if args.live else None)  # --live: no Tiger
    report["meta"]["analysis_id"] = runlog.analysis_id  # joins this report to its Tiger rows
    _start_events(args, report, runlog.analysis_id)
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
    events.stage("precheck", test=args.test, head=head[:7], good=good[:7], runs=N)
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
    # The failure to explain: HEAD's most common failure signature. Not just the first failing run's, which
    # may be a timeout (never TARGET_FAIL, so no state would ever match) or a rarer second failure mode.
    from collections import Counter

    sigs = Counter(r.signature for r in head_res if r.outcome != PASS and r.signature != "timeout")
    if not sigs:
        print("precheck: HEAD only ever times out → abort: no failure output to match states against")
        report.note("Aborted: the test only timed out at HEAD, so there is no failure signature to bisect on.")
        return _finish(report, args, 2)
    target_sig = sigs.most_common(1)[0][0]
    report["precheck"]["signature"] = target_sig
    events.emit("failure", signature=target_sig,
                error=report_error_line(next(r.output for r in head_res if r.signature == target_sig)))
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
    events.stage("precheck", "done", mode=mode, runs_per_state=n)
    if not args.line:
        found = line_from_traceback(next(r.output for r in head_res if r.signature == target_sig))
        if found:
            args.line = report["meta"]["line"] = found
            print(f"blame hint: {found}, from the traceback (pass --line to override)")

    # Stage 1 — guilty commit (blame hint, then git bisect)
    rp = Replayer(".", [], args.test, target_sig=target_sig, sandbox=sandbox, runlog=runlog, workers=args.workers)
    # flaky: n runs per state from the precheck; any target failure counts (replay.vote)
    guilty = blamed = None
    events.stage("commit", good=good[:7], head=head[:7])
    if args.line:
        path, ln = args.line.rsplit(":", 1)
        c = _git("blame", "-L", f"{ln},{ln}", "--porcelain", "HEAD", "--", path).split()[0]
        blamed = c
        events.emit("blame_commit", line=args.line, commit=c[:7], subject=_git("log", "-1", "--format=%s", c),
                    code=_git("show", f"HEAD:{path}").splitlines()[int(ln) - 1:int(ln)] or [""])
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
        events.emit("commits", good=good[:7], commits=[
            {"sha": c[:7], "subject": _git("log", "-1", "--format=%s", c)[:80]} for c in commits])
        # Same search as the edit-level bisect: a commit that crashes or fails differently (OTHER) is
        # never evidence, so the nearest other commit is tried instead.
        k = prefix_bisect(commits, lambda prefix: outcome(rp, "commit", State(prefix[-1], [], head), n))
        if k is None:
            print("commit: bisect inconclusive (the commits around the boundary crash or fail differently)")
            report.note("Commit bisect inconclusive: every commit around the boundary was OTHER.")
            return _finish(report, args, 2)
        guilty = commits[k - 1]
        print(f"commit: bisected → {guilty[:7]}")

    events.stage("commit", "done", guilty=guilty[:7], method="blame" if guilty == blamed else "bisect",
                 subject=_git("log", "-1", "--format=%s", guilty))
    events.stage("entire", commit=guilty[:7])
    cp = entire.checkpoint_id(guilty)
    report["commit"] = {"sha": guilty, "method": "blame" if guilty == blamed else "bisect", "blamed": blamed,
                        "blame_confirmed": guilty == blamed if blamed else None, "checkpoint": cp, "human": cp is None}
    if cp is None:
        events.stage("entire", "skip", why="no Entire checkpoint: a person wrote this commit")
        print(f"commit {guilty[:7]} has no Entire checkpoint → human-written, stopping")
        return _finish(report, args, 0)
    print(f"checkpoint: {cp}")

    # Stages 2–6 (A): session -> replayable edits -> blame hint -> pruning -> bisect -> confirm
    from dataclasses import asdict

    from .adapters import load_replayable
    from .pipeline import Options, find_root_cause

    try:
        steps, fid = load_replayable(guilty)
        events.stage("entire", "done", checkpoint=cp, sessions=len({s.session_id for s in steps if s.session_id}))
    except Exception as e:  # noqa: BLE001 — report it, don't crash
        events.stage("entire", "error", why=str(e)[:200])
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
    p = argparse.ArgumentParser(prog="agentwhy", description="Find which agent decision broke a test, and prove it.")
    p.add_argument("--test", help="command that runs one failing test (exit 0 = pass); omit it to be guided: "
                   "agentwhy lists the failing tests and asks which one")
    p.add_argument("--good", help="commit where the test passes (default: found by running the test on earlier commits)")
    p.add_argument("--line", help="file:line the failure points at (blame hint)")
    p.add_argument("--runs", type=int, default=5, help="reruns per arm (default 5)")
    p.add_argument("--setup", help="environment setup command, run once when building the sandbox image")
    p.add_argument("--sandbox", choices=["docker", "local"], default="docker",
                   help="run tests in throwaway Docker containers (default) or directly on the host")
    p.add_argument("--no-rerun", action="store_true", help="stop after root cause")
    p.add_argument("--lesson-attempts", type=int, default=3,
                   help="lessons to try when one doesn't help (INEFFECTIVE/PROMISING): each retry writes a different "
                        "lesson, told what failed, and reruns only the with-lesson arm (default 3; 1 = no retries)")
    p.add_argument("--no-prune", action="store_true", help="skip Graph pruning (for the pruned-vs-blind comparison)")
    p.add_argument("--scan", action="store_true", help="test every prefix instead of bisecting (sessions < 20 edits)")
    p.add_argument("--check-entire", action="store_true", help="smoke-test the Entire connection and exit")
    p.add_argument("--report", default=".whyagent/report.html", help="where to write the HTML report (+ .json)")
    p.add_argument("--open", action="store_true", help="open the HTML report in a browser when done")
    p.add_argument("--serve", action="store_true",
                   help="run the results site: every past analysis, live progress, start new ones (localhost only)")
    p.add_argument("--port", type=int, default=8700, help="--serve: port (default 8700)")
    p.add_argument("--id", help=argparse.SUPPRESS)  # --serve passes the analysis id it will show
    p.add_argument("--render", metavar="JSON", help="render a saved report .json to HTML and exit")
    p.add_argument("--rerun-agent", choices=["auto", "gemini-cli", "claude-code", "stub"],
                   help="agent for counterfactual reruns (default: auto with --model gemini, i.e. Gemini CLI, or "
                        "Claude Code if Gemini is unavailable or runs out of quota; claude-code with --model claude. "
                        "claude-code needs CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` or ANTHROPIC_API_KEY; "
                        "stub runs $WHYAGENT_STUB_CMD, no model, for rehearsing Stage 8)")
    p.add_argument("--target-runs", type=int, help="runs of the target test per rerun (default: the precheck's runs "
                                                    "per state, so flaky bugs need to stay fixed; 1 with --root-cause)")
    p.add_argument("--workers", type=int, default=2, help="sandboxes in parallel, for test runs and reruns (default 2; 4+ on a Vultr VM)")
    p.add_argument("--chart", action="store_true", help="write the Tiger Data pruning chart (.whyagent/chart.html)")
    p.add_argument("--trail", action="store_true",
                   help="post the root cause as a finding on this branch's Entire trail, with the revert as a suggested patch")
    p.add_argument("--lesson", help="test this lesson in the reruns instead of writing one with --model (Stage 7)")
    p.add_argument("--root-cause", metavar="JSON", help="dev: run stages 7-8 from a hard-coded RootCause fixture")
    p.add_argument("--live", action="store_true",
                   help="after the root cause: write a rule from the reruns, approve it, add it to --instructions, "
                        "rewind, rerun the agent with it and verify (replaces stages 7-8; never touches Brain or Tiger)")
    p.add_argument("--yes", action="store_true", help="--live: approve the rule without asking")
    p.add_argument("--instructions", default="AGENTS.md",
                   help="--live / --pr: the agent instructions file the rule is added to (default AGENTS.md)")
    p.add_argument("--pr", dest="pr", action="store_true", default=True,
                   help="(the default) after the root cause, open a GitHub PR with a fix verified at HEAD: if Stage 8 "
                        "VALIDATES the lesson, a passing with-lesson rerun's work (else the revert of the guilty edit) "
                        "plus the rule in --instructions; otherwise the revert, with no rule. Needs a github.com "
                        "remote and `gh` logged in")
    p.add_argument("--no-pr", dest="pr", action="store_false", help="don't open a PR")
    p.add_argument("--pr-remote", help="--pr: the git remote to push the PR branch to (default: the first github.com one)")
    p.add_argument("--brain", action="store_true", help="list the validated lessons (Tiger Data) and exit")
    p.add_argument("--rules", action="store_true",
                   help="print the validated rules as Markdown for an agent's context, skipping ones already in "
                        "--instructions, and exit (for a SessionStart hook; prints nothing if there are none)")
    p.add_argument("--rules-limit", type=int, default=10, help="--rules: at most this many, newest first (default 10)")
    p.add_argument("--install-hook", action="store_true",
                   help="add a Claude Code SessionStart hook that runs --rules, so every session (any repo) starts with "
                        "your validated rules; to the settings in --claude-config, $CLAUDE_CONFIG_DIR or ~/.claude")
    p.add_argument("--uninstall-hook", action="store_true",
                   help="remove that hook (do this before recording a demo session: the rules would steer the agent)")
    p.add_argument("--claude-config", metavar="DIR", help="--install-hook / --uninstall-hook: Claude Code config dir")
    p.add_argument("--model", choices=["gemini", "claude", "mock"],
                   help="who writes the lesson (Stage 7) or, with --live, the rule (default $WHYAGENT_MODEL, else "
                        "gemini). claude = `claude -p` with CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY, model "
                        "$WHYAGENT_CLAUDE_MODEL (e.g. haiku); mock = canned responses for the seeded demo repo, --live only")
    args = p.parse_args(argv)
    import shlex
    shown = list(sys.argv[1:] if argv is None else argv)
    if "--id" in shown:  # --serve's own bookkeeping: rerunning with it would overwrite that analysis
        i = shown.index("--id")
        del shown[i:i + 2]
    args.command = "agentwhy " + shlex.join(shown)  # shown on the report
    if args.live and (args.trail or args.lesson or args.no_rerun or "--pr" in shown):
        p.error("--live can't be combined with --trail, --lesson, --no-rerun or --pr")
    if args.live:
        args.pr = False  # the live loop writes its rule to --instructions itself
    if (args.yes or args.model == "mock") and not args.live:
        p.error("--yes and --model mock only apply to --live")
    from .model import DEFAULT_AGENT, model_name
    args.rerun_agent = args.rerun_agent or DEFAULT_AGENT.get(model_name(args.model), "gemini-cli")
    if args.check_entire:
        return cmd_check_entire(args)
    if args.brain:
        return cmd_brain()
    if args.install_hook or args.uninstall_hook:
        from .rules import claude_settings_path, hook_command, install_hook, uninstall_hook
        path = claude_settings_path(args.claude_config)
        if args.uninstall_hook:
            print(f"hook: {'removed from' if uninstall_hook(path) else 'not found in'} {path}")
            return 0
        print(f"hook: {install_hook(path)} in {path}\n  SessionStart → {hook_command()}\n"
              "  New Claude Code sessions start with your validated rules. Before recording a demo session, run "
              "agentwhy --uninstall-hook (the rules would steer the agent away from the mistake).")
        return 0
    if args.rules:
        from .rules import rules_markdown
        text = rules_markdown(instructions=args.instructions, limit=args.rules_limit)
        if text:
            print(text)
        return 0
    if args.serve:
        from .server import serve
        return serve(_history(args), args.port, open_browser=args.open)
    if args.render:
        return cmd_render(args)
    if args.chart:
        return cmd_chart(args)
    if args.root_cause:
        return cmd_root_cause(args)
    from . import start
    if not args.test:
        if not sys.stdin.isatty():
            p.error("--test is required when not run interactively (run plain `agentwhy` in a terminal to be "
                    "guided)")
        if not start.guided(args, _make_sandbox):
            return 2
    elif not args.good and not start.auto_good(args, _make_sandbox):
        return 2
    return cmd_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
