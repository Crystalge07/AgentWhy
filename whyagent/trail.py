"""--trail: post the proven root cause as a finding on the branch's Entire trail.

The finding sits on the guilty line, carries the proof, the agent's reasoning and the lesson, and
attaches the revert of the guilty edit as a suggested patch the reviewer can apply in one click.
"""
from __future__ import annotations

import difflib
import os
import subprocess
import tempfile

from .types import RootCause


def _head_text(path: str, cwd: str = ".") -> str | None:
    p = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=cwd, capture_output=True, encoding="utf-8",
                       errors="replace")
    return p.stdout if p.returncode == 0 else None


def revert_patch(rc: RootCause, head_text: str | None) -> tuple[str, int] | None:
    """Unified diff that undoes the guilty edit in the file as it is at HEAD, and the line it starts on.
    None when the edit's text isn't in the HEAD file exactly once (later edits changed it)."""
    s = rc.step
    if head_text is None or not s.new_text or head_text.count(s.new_text) != 1:
        return None
    fixed = head_text.replace(s.new_text, s.old_text, 1)
    line = head_text[:head_text.index(s.new_text)].count("\n") + 1
    diff = difflib.unified_diff(head_text.splitlines(keepends=True), fixed.splitlines(keepends=True),
                                f"a/{s.file}", f"b/{s.file}")
    return "".join(diff), line


def finding_body(rc: RootCause, report: dict) -> str:
    s = rc.step
    test = (report.get("meta") or {}).get("test", "the test")
    test = next((w for w in test.split() if "::" in w), test)
    pre = report.get("precheck") or {}
    bh = report.get("blame_hint") or {}
    lines = [f"**whyagent: this agent edit broke `{test}`**", "",
             "Proven by test runs: the test passes right before this edit, fails right after it, and passes "
             f"again when only this edit is reverted ({len(report.get('runs') or [])} test runs, {pre.get('mode', '?')} mode).",
             "Tests were pinned to HEAD, so edits the agent made to tests don't hide the bug."]
    if bh and not bh.get("is_cause"):
        lines.append(f"`git blame` on `{bh.get('line')}` points at a different edit; reverting it still fails.")
    lines.append("")
    if s.prompt:
        lines += [f"**Prompt in effect:** {s.prompt.strip()[:500]}", ""]
    if s.command:
        lines += ["**Made from the shell:**", "```", s.command.strip()[:800], "```", ""]
    if s.reasoning:
        lines += ["**Agent's reasoning right before the edit:**", "> " + s.reasoning.strip()[:800].replace("\n", "\n> "), ""]
    ls, rr = report.get("lesson"), report.get("rerun")
    if ls:
        lines.append(f"**Lesson:** {ls.get('text')}")
        if rr and rr.get("verdict"):
            a, b = rr.get("a") or [], rr.get("b") or []
            lines.append(f"Counterfactual reruns ({rr.get('agent')}): without lesson {a.count('PASS')}/{len(a)} pass, "
                         f"with lesson {b.count('PASS')}/{len(b)} pass → {rr['verdict']}"
                         + (f" (p = {rr['p']:.3f})" if rr.get("p") is not None else ""))
        lines.append("")
    lines.append(f"Session `{s.session_id}`, edit #{s.id}. The suggested change reverts this edit.")
    return "\n".join(lines)


def post_finding(rc: RootCause, report: dict, client_id: str, cwd: str = ".", run=None) -> str:
    """Add the finding with `entire trail finding add`; returns the CLI's output (it includes the URL)."""
    from . import entire

    run = run or entire._run
    patch = revert_patch(rc, _head_text(rc.step.file, cwd))
    line = patch[1] if patch else next((d.get("line") for d in (report.get("session") or {}).get("steps", [])
                                       if d.get("id") == rc.step.id), None)
    args = ["trail", "finding", "add", "--severity", "high", "--confidence", "1.0", "--file", rc.step.file,
            "--client-id", client_id, "-m", finding_body(rc, report)]
    if line:
        args += ["--line", str(line)]
    if not patch:
        return run(args, cwd=cwd)
    fd, path = tempfile.mkstemp(suffix=".diff")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(patch[0])
        return run(args + ["--patch-file", path], cwd=cwd)
    finally:
        os.unlink(path)
