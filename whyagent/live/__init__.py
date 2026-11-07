"""The live loop: diagnosis → rule → approve → rewind → rerun → verify, in one command (`whyagent --live`).

Starts from a proven RootCause (stages 1–6, or a saved root_cause.json). It never reads or writes Brain:
one passing rerun isn't evidence a rule works, so only a VALIDATED Stage 8 result may be saved there.
Every stage either succeeds or raises LiveLoopError(stage, why); there are no silent fallbacks.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..model import AGENT_INSTRUCTIONS, MockModel, Model, ModelError
from ..rerun import Reruns
from ..rerun.harness import FAIL, OTHER, PASS, REGRESSED, TAMPERED
from ..types import TARGET_FAIL, RootCause
from .instructions import append_rule, has_rule, provenance, worktree_files
from .rule import evidence, files_changed, write_rule

STAGES = ["diagnose", "rule", "approve", "rewind", "rerun", "verify"]


class LiveLoopError(Exception):
    def __init__(self, stage: str, why: str):
        super().__init__(f"{stage}: {why}")
        self.stage, self.why = stage, why


@dataclass
class LiveOptions:
    test: str
    instructions: str = "AGENTS.md"
    runs: int = 5            # no-rule reruns for the evidence
    yes: bool = False        # skip the approval prompt
    workers: int = 2
    target_runs: int = 1
    checkpoint: str | None = None  # Entire checkpoint of the guilty commit, for display
    repo: str = "."


class Printer:
    def __init__(self, out: Callable[[str], None] = print):
        self.out = out

    def stage(self, i: int, name: str, title: str):
        self.out(f"\n── {i}/{len(STAGES)} {name} · {title} " + "─" * max(0, 48 - len(name) - len(title)))

    def line(self, text: str = ""):
        self.out(f"   {text}" if text else "")

    def ok(self, text: str):
        self.out(f"   ✓ {text}")


def _first(text: str | None, n: int = 110) -> str:
    t = (text or "").strip().splitlines()
    t = t[0] if t else ""
    return t if len(t) <= n else t[:n - 1] + "…"


def _error_line(output: str) -> str:
    """The last exception line of a pytest failure, for the before/after table."""
    for ln in reversed((output or "").strip().splitlines()):
        s = ln.strip()
        if s.startswith("E ") or "Error" in s.split(":")[0]:
            return _first(s.removeprefix("E").strip(), 56)
    return _first((output or "").strip().splitlines()[-1] if output and output.strip() else "", 56)


def run_live(rc: RootCause, rp, model: Model, opts: LiveOptions, out: Callable[[str], None] = print,
             ask: Callable[[str], str] | None = None) -> dict:
    """Run the loop. Returns a summary dict (for report.json); raises LiveLoopError on any failed stage."""
    p = Printer(out)
    summary: dict = {"model": model.name, "instructions": str(opts.instructions)}
    interactive = ask is None
    ask = ask or input
    if interactive and not opts.yes and not sys.stdin.isatty():  # check before any reruns, not at the prompt
        raise LiveLoopError("approve", "stdin isn't a terminal, so I can't ask for approval; pass --yes")

    # ---------------------------------------------------------------- 1 diagnose
    p.stage(1, "diagnose", "culprit + counterfactual reruns")
    s = rc.step
    p.line(f"culprit: edit #{s.id} {s.file} (session {s.session_id[:8]})")
    for ln in [ln for ln in s.new_text.splitlines() if ln.strip()][:3]:
        p.line(f"    + {_first(ln, 90)}")
    if s.reasoning:
        p.line(f"agent's reasoning: {_first(s.reasoning)}")
    if isinstance(model, MockModel):
        try:
            sc = model.bind(rc)
        except ModelError as e:
            raise LiveLoopError("diagnose", str(e)) from None
        p.line(f"model: mock (canned responses: {sc['name']})")
    else:
        p.line(f"model: {model.name}")
    rp.stage = "live-before"
    before = rp.run(rc.head_state, 1)[0]
    if before.outcome == PASS:
        raise LiveLoopError("diagnose", "the target test passes at the analyzed commit; nothing to fix")
    if before.outcome != TARGET_FAIL:
        raise LiveLoopError("diagnose", f"the analyzed commit fails differently than recorded ({before.outcome}): "
                                        + _error_line(before.output))
    p.ok(f"target test fails at the analyzed commit: {_error_line(before.output or rc.failing_output)}")
    summary["before"] = {"outcome": before.outcome, "error": _error_line(before.output or rc.failing_output)}

    try:
        agent = model.agent(None)
    except ModelError as e:
        raise LiveLoopError("diagnose", str(e)) from None
    rr = Reruns(rp, agent, workers=opts.workers, target_runs=opts.target_runs)
    rr.capture_diff = True
    p.line(f"rerunning the agent {opts.runs}× from just before edit #{s.id}, no rule…")
    results = rr.run_arm(rc, None, opts.runs, "A")
    for r in results:
        files = ", ".join(files_changed(r.diff)) or "nothing"
        p.line(f"  run {r.run_index}: {r.outcome:<9} changed {files}" + (f" · {r.detail}" if r.outcome != PASS else ""))
    valid = [r for r in results if r.outcome != OTHER]
    if not valid:
        raise LiveLoopError("diagnose", f"no valid reruns, the agent never really ran: {results[0].detail}")
    if rr.excluded:
        p.line(f"({len(rr.excluded)} test(s) the session added aren't required of reruns)")
    summary["counterfactual"] = [{"run": r.run_index, "outcome": r.outcome, "detail": r.detail,
                                  "files": list(files_changed(r.diff))} for r in results]

    # ---------------------------------------------------------------- 2 rule
    p.stage(2, "rule", "what the passing runs did differently")
    ev = evidence(results)
    if not ev.passed:
        raise LiveLoopError("rule", f"none of the {len(valid)} valid reruns passed, so there's no contrast to learn "
                                    "from (try more --runs)")
    if not ev.failed:
        raise LiveLoopError("rule", f"all {len(valid)} valid reruns passed without a rule; the agent doesn't need one")
    for ln in ev.lines():
        p.line(ln)
    try:
        rule = write_rule(rc, ev, model.generate)
    except Exception as e:  # noqa: BLE001 — any model failure stops the loop here, loudly
        raise LiveLoopError("rule", f"{model.name} failed: {type(e).__name__}: {_first(str(e), 200)}") from None
    if len(rule.strip(" .")) < 8:
        raise LiveLoopError("rule", f"{model.name} returned no usable rule ({rule!r})")
    p.line("")
    p.line(f"proposed rule: “{rule}”")
    summary["evidence"] = ev.as_dict()

    # ---------------------------------------------------------------- 3 approve (+ write)
    path = Path(opts.instructions)
    p.stage(3, "approve", f"add the rule to {path.name}")
    if opts.yes:
        p.ok("approved (--yes)")
    else:
        rule = _approve(rule, path, ask, p)
        if rule is None:
            summary["approved"] = False
            raise LiveLoopError("approve", f"rule rejected; nothing was written to {path}")
    summary.update(rule=rule, approved=True)
    comment = provenance(opts.test, s.session_id, s.id, s.file)
    try:
        written = append_rule(path, rule, comment)
    except OSError as e:
        raise LiveLoopError("approve", f"couldn't write {path}: {e}") from None
    if written:
        p.ok(f"written to {path}")
        p.line(f"    {comment}")
    else:
        p.ok(f"already in {path}, not added again")
    summary["written"] = written

    # ---------------------------------------------------------------- 4 rewind
    p.stage(4, "rewind", f"restore the state just before edit #{s.id}")
    try:
        d = rp.build(rc.before_state)
    except Exception as e:  # noqa: BLE001
        raise LiveLoopError("rewind", f"couldn't rebuild the state before edit #{s.id}: {_first(str(e), 200)}") from None
    try:
        b = rc.before_state
        p.line(f"commit {b.base_commit[:8]} + edits "
               + (", ".join(f"#{i}" for i in b.step_ids) if b.step_ids else "(none)")
               + f" replayed from session {s.session_id[:8]}")
        if opts.checkpoint:
            p.line(f"Entire checkpoint {opts.checkpoint}")
        p.line(f"isolated worktree: {d}")
        try:
            files = worktree_files(path, opts.repo, b.base_commit, agent.name, AGENT_INSTRUCTIONS)
        except OSError as e:
            raise LiveLoopError("rewind", f"couldn't read {path}: {e}") from None
        if not has_rule(files[AGENT_INSTRUCTIONS], rule):
            raise LiveLoopError("rewind", f"the rule isn't in the {AGENT_INSTRUCTIONS} the agent would read")
        p.ok(f"{AGENT_INSTRUCTIONS} with the rule placed in the worktree"
             + (" (+ CLAUDE.md importing it)" if "CLAUDE.md" in files else "") + "; your checkout is untouched")

        # ------------------------------------------------------------ 5 rerun
        p.stage(5, "rerun", "the agent redoes the task with the rule")
        try:
            rr.agent = model.agent(rule)
        except ModelError as e:
            raise LiveLoopError("rerun", str(e)) from None
        p.line(f"agent: {rr.agent.name}; prompt: the original task, no hint (the rule is only in {AGENT_INSTRUCTIONS})")
        with rr.egress():
            res = rr.run_in(d, rc, None, "live", files)
        if res.outcome == OTHER:
            raise LiveLoopError("rerun", f"the agent didn't run: {res.detail}")
        changed = files_changed(res.diff)
        p.ok(f"agent finished in {res.duration_ms / 1000:.1f}s, changed {', '.join(changed) or 'nothing'}")
        for ln in [ln for ln in res.diff.splitlines()
                   if ln[:1] in "+-" and not ln.startswith(("+++", "---")) and ln[1:].strip()][:8]:
            p.line(f"    {_first(ln, 100)}")
    finally:
        rp.teardown(d)

    # ---------------------------------------------------------------- 6 verify
    p.stage(6, "verify", "target test, before vs after")
    after_ok = res.outcome == PASS
    rows = [
        ("", "before (analyzed commit)", "after (rerun with rule)"),
        ("target test", "FAIL", "PASS" if after_ok else ("FAIL" if res.outcome == FAIL else "—")),
        ("error", summary["before"]["error"], "—" if after_ok else res.detail),
        ("other tests", "—", "broke some" if res.outcome == REGRESSED else
         (f"{len(rr._baseline or ())} still pass" if rr._baseline else "not checked")),
        ("test files", "—", "edited" if res.outcome == TAMPERED else "untouched"),
    ]
    w0, w1 = max(len(r[0]) for r in rows), max(len(r[1]) for r in rows)
    for a, b_, c in rows:
        p.line(f"{a:<{w0}}  {b_:<{w1}}  {c}")
    p.line("")
    if after_ok:
        p.ok("rerun passed (unvalidated)")
        p.line("One rerun isn't proof the rule works. Stage 8 tests it A/B: whyagent --root-cause … --lesson \"<rule>\"")
    else:
        p.out(f"   ✗ rerun still fails: {res.outcome.lower()}, {res.detail}")
    summary["after"] = {"outcome": res.outcome, "detail": res.detail, "files": list(changed),
                        "label": "rerun passed (unvalidated)" if after_ok else "rerun still fails"}
    return summary


def _approve(rule: str, path: Path, ask: Callable[[str], str], p: Printer) -> str | None:
    while True:
        p.line(f"“{rule}”")
        try:
            a = ask(f"   Write this rule to {path}? [y]es / [n]o / [e]dit: ").strip().lower()
        except EOFError:
            raise LiveLoopError("approve", "no answer (stdin closed); pass --yes to approve") from None
        if a in ("y", "yes"):
            p.ok("approved")
            return rule
        if a in ("n", "no"):
            return None
        if a in ("e", "edit"):
            new = _edit(rule, ask)
            if new:
                rule = new
            else:
                p.line("(empty, kept the previous rule)")
            continue
        p.line("please answer y, n or e")


def _edit(rule: str, ask: Callable[[str], str]) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        try:
            return ask("   New rule: ").strip()
        except EOFError:
            return ""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(rule + "\n")
    try:
        cmd = shlex.split(editor, posix=os.name != "nt") + [f.name]
        if subprocess.run(cmd).returncode != 0:
            raise LiveLoopError("approve", f"editor {editor!r} exited with an error")
        text = Path(f.name).read_text(encoding="utf-8")
    finally:
        os.unlink(f.name)
    return " ".join(ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#"))
