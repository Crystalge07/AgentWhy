"""Stages 2-6 (owner A): from the guilty commit's session to a proven root-cause edit.

An edit is the root cause only if the test passes right before it, fails right after it, and
passes again when that edit is reverted from the final code. Every stage here establishes one of
those facts with real test runs, through `Replayer.run` only (the A/B contract).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from . import graph
from .adapters.fidelity import Fidelity, apply
from .bisect import (blame_hint, closure, confirm, ddmin, deps, line_owners, map_line, prefix_bisect_parallel,
                     scan)
from .replay import Replayer, is_test_file, vote
from .types import OTHER, PASS, TARGET_FAIL, RootCause, State, Step


@dataclass
class Options:
    line: str | None = None  # "path:line" at HEAD, the blame hint
    n: int = 1               # runs per state; more in flaky mode (see decide())
    prune: bool = True
    scan: bool = False
    workers: int = 1         # >1 only pays off with B's parallel worker pool
    ddmin_cap: int = 20


_EXC_LINE = re.compile(r"^E\s|\b\w*(Error|Exception)\b|^AssertionError|^assert ")
_SIGNAL = re.compile(r"Traceback|FAILED|refused|denied|not found|No such|cannot|can't", re.I)
_SUMMARY = re.compile(r"^=*\s*\d+ (failed|passed|errors?)\b|^=+ .* =+$|^short test summary")


def trigger_line(trigger: str | None) -> str:
    """The most telling line of a trigger: the last exception line, else another error-looking
    line, else the last line. pytest's summary lines ("1 failed in 0.3s") never win."""
    lines = [ln.strip() for ln in (trigger or "").splitlines() if ln.strip() and not _SUMMARY.match(ln.strip())]
    for pat in (_EXC_LINE, _SIGNAL):
        hits = [ln for ln in lines if pat.search(ln)]
        if hits:
            return hits[-1][:160]
    return (lines[-1] if lines else "")[:160]


decide = vote  # one flaky-mode rule for everyone: replay.vote


def _show(repo: str, rev: str, path: str) -> str | None:
    p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=repo, capture_output=True)
    return p.stdout.decode("utf-8", errors="replace") if p.returncode == 0 else None


def _step_dicts(steps: list[Step], read_parent: Callable[[str], str | None]) -> list[dict]:
    """Steps for the report, with the line each edit lands on in the file as it was at that point."""
    cur: dict[str, str | None] = {}
    out = []
    for s in steps:
        if s.file not in cur:
            cur[s.file] = read_parent(s.file)
        text = cur[s.file] or ""
        i = text.find(s.old_text) if s.old_text else 0
        line = text[:max(i, 0)].count("\n") + 1
        try:
            cur[s.file] = apply(cur[s.file], s)
        except ValueError:
            pass
        out.append({"id": s.id, "file": s.file, "line": line, "kind": s.kind, "old_text": s.old_text,
                    "new_text": s.new_text, "reasoning": s.reasoning, "trigger": s.trigger,
                    "command": s.command or None})
    return out


def find_root_cause(repo: str, steps: list[Step], fid: Fidelity, guilty: str, parent: str, head: str,
                    rp: Replayer, opts: Options, report, say=print, runlog=None) -> RootCause | None:
    """`rp` must be built with `steps` and the target signature; B's stages 7-8 reuse it afterwards."""
    by_id = {s.id: s for s in steps}
    order = [s.id for s in steps if not is_test_file(s.file)]  # test edits are pinned, never bisected
    read_parent = lambda f: _show(repo, parent, f)
    test_cmd = rp.test_cmd
    failing_output = [""]

    def record(stage: str, ids: list[int], res) -> str:
        report.log_runs(stage, f"{parent[:7]}+{len(ids)} edits", res)
        for r in res:
            if r.outcome == TARGET_FAIL and not failing_output[0]:
                failing_output[0] = r.output
        return vote([r.outcome for r in res])

    def run(stage: str):
        def go(ids: list[int]) -> str:
            rp.stage = stage  # tags Tiger rows
            return record(stage, ids, rp.run(State(parent, list(ids), head), opts.n))
        return go

    def run_many(stage: str):
        """Several prefixes at once on B's worker pool (Replayer.run_many)."""
        def go(prefixes: list[list[int]]) -> list[str]:
            rp.stage = stage
            results = rp.run_many([State(parent, list(p), head) for p in prefixes], opts.n)
            return [record(stage, p, res) for p, res in zip(prefixes, results)]
        return go

    # Stage 2: the session as replayable edits + dependency map
    sessions = sorted({s.session_id for s in steps if s.session_id})
    report["session"] = {"id": ", ".join(sessions) or "?", "agent": next((s.agent for s in steps if s.kind != "unattributed"), "?"),
                         "prompt": next((s.prompt for s in steps if s.prompt), ""), "replay_matches": fid.matches,
                         "unattributed": bool(fid.unattributed), "unattributed_ids": fid.unattributed,
                         "shell_ids": fid.shell,
                         "dropped": fid.dropped, "steps": _step_dicts(steps, read_parent)}
    say(f"session: {report['session']['id']} · {len(order)} edits · "
        + ("replay matches commit ✓" if fid.matches else
           "replay matches commit ✓ with " + ", ".join(p for p in (
               f"{len(fid.shell)} edit(s) made by the agent's shell commands" if fid.shell else "",
               f"{len(fid.unattributed)} unattributed edit(s)" if fid.unattributed else "") if p)
           + f" ({', '.join(fid.files)})"))
    if not order:
        report.note("The session has no edits outside test files, so there is nothing to bisect.")
        return None
    nontest = [by_id[i] for i in order]
    dep = deps(nontest, base={f: read_parent(f) or "" for f in {s.file for s in nontest}})

    root = None
    collateral: list[int] = []
    probes: list[dict] = []

    # Stage 3: edit-level blame hint
    if opts.line:
        path, ln = opts.line.rsplit(":", 1)
        path = path.replace("\\", "/")
        head_text, g_text = _show(repo, head, path), _show(repo, guilty, path)
        gl = map_line(head_text, g_text, int(ln)) if head_text is not None and g_text is not None else None
        owners = line_owners(steps, path, read_parent(path)) if gl else []
        owner = owners[gl - 1] if gl and gl <= len(owners) else None
        if owner is None or owner not in order:
            why = ("the line changed after the guilty commit" if gl is None else
                   "the line wasn't written by this session")
            report.note(f"Blame hint skipped: {why} ({opts.line}).")
            say(f"blame hint: skipped ({why})")
        else:
            bh = blame_hint(order, owner, dep, run("blame_hint"))
            report["blame_hint"] = {"line": opts.line, "step_ids": [owner], "outcome": bh["outcome"],
                                    "is_cause": bh["is_cause"], "reverted": bh["reverted"]}
            s = by_id[owner]
            if bh["is_cause"]:
                root, collateral = owner, bh.get("collateral", [])
                k = order.index(owner)
                probes = [{"after_step": order[k - 1] if k else 0, "outcome": bh["before"]},
                          {"after_step": owner, "outcome": bh["after"]}]
                say(f"blame hint: edit #{owner} touches {opts.line} → revert, test passes; boundary confirmed ✓ root cause")
            else:
                say(f"blame hint: edit #{owner} touches {opts.line} ({s.file}) → NOT the cause (revert, still fails)")

    kept = list(order)
    if root is None:
        # Stage 4: Graph pruning, always verified with one run
        if opts.prune:
            files = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", guilty], cwd=repo,
                                       capture_output=True, encoding="utf-8", errors="replace").stdout.split())
            reach = graph.reachable(test_cmd, files, lambda f: _show(repo, guilty, f))
            if reach is None:
                report.note("Pruning skipped: --test names no test file to trace imports from.")
            else:
                cands = graph.prune_candidates(steps, order, reach, dep)
                if cands:
                    verdict = run("prune")([i for i in order if i not in cands])
                    verified = verdict == TARGET_FAIL
                    if verified:
                        kept = [i for i in order if i not in cands]
                        if runlog is not None:
                            runlog.set_pruned(True)  # Tiger chart: this analysis ran with pruning
                    report["pruning"] = {"pruned": cands if verified else [], "candidates": cands,
                                         "verified": verified, "source": graph.SOURCE,
                                         "runs_with": None, "runs_without": None}
                    say(f"pruning: {len(cands)} edit(s) unreachable from the test → "
                        + ("still fails without them ✓ pruned" if verified else
                           f"{verdict} without them → graph missed a dependency, pruning dropped"))
                else:
                    say("pruning: every edit is reachable from the test, nothing to prune")

        # Stage 5: prefix bisect
        def probe_many(prefixes):
            outs = run_many("bisect")(prefixes)
            probes.extend({"after_step": p[-1] if p else 0, "outcome": r} for p, r in zip(prefixes, outs))
            return outs

        runs_before = rp.runs
        if opts.scan:
            res = scan(kept, probe_many)
            k = res["first"]
            if res["flips"]:
                report.note(f"--scan: the test flips across the session; boundaries after edits "
                            f"{', '.join('#' + str(kept[b - 1]) for b in res['boundaries'])}. Using the first.")
        else:
            k, _rounds = prefix_bisect_parallel(kept, probe_many, workers=opts.workers)
        report["bisect"] = {"probes": probes, "boundary": kept[k - 1] if k else None, "scan": opts.scan,
                            "edits": len(kept), "kept": kept}
        if k is None:
            say("bisect: inconclusive (every state around the boundary crashed or failed differently)")
            report.note("Bisect inconclusive: the states around the boundary were all OTHER.")
            return None
        root = kept[k - 1]
        # Bisect can infer P(k-1) = PASS and P(k) = TARGET_FAIL from the endpoints without running them.
        # Run them explicitly so the report shows both facts as test results (usually cache hits).
        have = {p["after_step"] for p in probes}
        missing = [kept[:i] for i in (k - 1, k) if (kept[i - 1] if i else 0) not in have]
        if missing:
            probe_many(missing)
        say(f"bisect: {len(kept)} edits → {rp.runs - runs_before} test runs → first failing prefix at edit #{root}")

        # Stage 6: confirm from the end, ddmin if more than one cause
        ok, coll = confirm(kept, root, dep, run("confirm"))
        collateral = sorted(coll)
        cf = {"step_id": root, "collateral": collateral, "outcome": PASS if ok else TARGET_FAIL, "ddmin": None}
        report["confirm"] = cf
        extra: list[int] = []
        if ok:
            say(f"confirm: revert #{root} from the final state → test passes ✓ root cause")
        else:
            dd = ddmin(kept, dep, run("ddmin"), cap=opts.ddmin_cap)
            cf["ddmin"] = sorted(dd) if dd else None
            if not dd:
                say(f"confirm: revert #{root} still fails → ddmin inconclusive")
                report.note("More than one cause, and ddmin couldn't isolate a minimal set within its run budget.")
                return None
            extra = sorted(dd - {root})
            say(f"confirm: revert #{root} still fails → ddmin: reverting {{{', '.join('#' + str(i) for i in sorted(dd))}}} passes")
    else:
        extra = []
        report["confirm"] = {"step_id": root, "collateral": collateral, "outcome": PASS, "ddmin": None}
        report["bisect"] = {"probes": probes, "boundary": root, "scan": False, "edits": len(order), "kept": order,
                            "via": "blame_hint"}  # no bisect ran: Stage 3 already tested the boundary

    s = by_id[root]
    idx = order.index(root)
    if not failing_output[0]:
        r = rp.run(State(parent, order, head), 1)[0]
        failing_output[0] = r.output
    rc = RootCause(step=s, collateral=collateral, before_state=State(parent, order[:idx], head),
                   edits_before=[by_id[i] for i in order[:idx]], failing_output=failing_output[0],
                   head_state=State(head, [], head), extra_causes=extra)
    report["root_cause"] = {"step_id": root, "failing_output": rc.failing_output, "collateral": collateral,
                            "extra_causes": extra, "file": s.file}
    line = next((d["line"] for d in report["session"]["steps"] if d["id"] == root), "?")
    say(f"root cause: edit #{root} {s.file}:{line}  {s.new_text.strip().splitlines()[0][:80] if s.new_text.strip() else '(deletion)'}")
    say(f"trigger: {trigger_line(s.trigger) or '(no command output right before this edit)'}")
    if s.command:
        say(f"command: {s.command.strip().splitlines()[0][:200]}  (the agent edited {s.file} from the shell)")
    say(f"reasoning: {(s.reasoning or '(not recorded: hidden thinking)').strip()[:200]}")
    report["runs_total"] = rp.runs
    return rc
