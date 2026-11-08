"""--pr: when Stage 8 VALIDATES a lesson, open a GitHub PR with a verified fix and the rule in AGENTS.md.

The fix is the work of a rerun that passed *with* the lesson. Reruns start just before the guilty edit, so
their diff can't go onto HEAD as it is: we rebuild the rerun's final tree, diff it against the guilty commit
G (which swaps the session's work from the guilty edit on for the rerun's), and 3-way apply that onto HEAD.
If no passing rerun applies and verifies there, we fall back to reverting the guilty edit.

Nothing is pushed unless the candidate passes the target test and loses no suite test *at HEAD*. Your
checkout is never touched: everything happens in a temporary worktree, and the commit goes straight to a
new branch on the GitHub remote.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .live.instructions import append_rule, provenance
from .replay import is_test_file
from .rerun.harness import PASS, passed_tests
from .trail import _head_text, revert_patch
from .types import RootCause

MAX_CANDIDATES = 3  # passing reruns to try at HEAD before falling back to the revert
_JUNK = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".whyagent/", ".ruff_cache/")
_AGENT_FILES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md"}  # the rule goes in separately; never take a rerun's copy


class FixError(Exception):
    pass


@dataclass
class Fix:
    kind: str          # "rerun" | "revert"
    patch: str         # applies to HEAD
    files: list[str]
    detail: str        # how it was verified, for the PR body
    run_index: int | None = None


def _git(cwd, *args, input: str | None = None, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", input=input)
    if check and p.returncode != 0:
        raise FixError(f"git {args[0]} failed: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def _keep(path: str, globs) -> bool:
    return not (is_test_file(path, globs) or path.endswith(".pyc") or Path(path).name in _AGENT_FILES
                or any(j in path + "/" for j in _JUNK))


def guilty_commit(repo: str, rc: RootCause) -> str | None:
    """The commit whose first parent is the rerun start state's base, on the way to HEAD."""
    parent, head = rc.before_state.base_commit, rc.head_state.base_commit
    for c in _git(repo, "rev-list", "--reverse", "--ancestry-path", f"{parent}..{head}").split():
        if _git(repo, "rev-parse", f"{c}^").strip() == parent:
            return c
    return None


def rerun_patch(rp, rc: RootCause, diff: str, guilty: str) -> tuple[str, list[str]]:
    """Patch from the guilty commit's code to the rerun's final code (non-test files only), and its files."""
    d = rp.build(rc.before_state)
    try:
        _git(d, "add", "-A")  # the same baseline the rerun diffed against
        _git(d, "apply", "--whitespace=nowarn", input=diff if diff.endswith("\n") else diff + "\n")
        _git(d, "add", "-A")
        files = [f for f in _git(d, "diff", "--cached", "--name-only", guilty).splitlines()
                 if f and _keep(f, rp.test_globs)]
        if not files:
            return "", []
        return _git(d, "diff", "--cached", "--full-index", "--binary", guilty, "--", *files), files
    finally:
        rp.teardown(d)


def verify_at_head(rp, rr, rc: RootCause, patch: str) -> tuple[str | None, str]:
    """Build HEAD, apply `patch`, run the target test and the suite. (worktree, "") on success — the caller
    tears it down — or (None, why it failed)."""
    d = rp.build(rc.head_state)
    try:
        p = subprocess.run(["git", "apply", "--3way", "--whitespace=nowarn"], cwd=d, input=patch,
                           capture_output=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            raise _Reject("doesn't apply cleanly at HEAD")
        sb = rp.sandbox
        for i in range(max(1, rr.target_runs)):
            ex = sb.exec(d, rp.test_cmd, rp.timeout)
            if ex.timed_out or ex.exit_code != 0:
                raise _Reject("target test still fails at HEAD" + (f" (run {i + 1})" if rr.target_runs > 1 else ""))
        if rr._baseline and rr.suite_cmd:
            lost = rr._baseline - passed_tests(sb.exec(d, rr.suite_cmd, rp.timeout).output)
            if lost:  # same flake guard as the reruns: a test must be lost twice
                lost &= rr._baseline - passed_tests(sb.exec(d, rr.suite_cmd, rp.timeout).output)
            if lost:
                raise _Reject("breaks " + ", ".join(sorted(lost)[:5]))
        return d, ""
    except _Reject as e:
        rp.teardown(d)
        return None, str(e)
    except BaseException:
        rp.teardown(d)
        raise


class _Reject(Exception):
    pass


def _verified(rr) -> str:
    n = len(rr._baseline or ())
    return "target test passes at HEAD" + (f", all {n} baseline suite tests still pass" if n else "") \
        + ", test files untouched"


def choose_fix(rp, rr, rc: RootCause, b_results, log=print) -> tuple[Fix, str]:
    """The first fix that verifies at HEAD: a passing with-lesson rerun (smallest first), else the revert.
    Returns the fix and the verified worktree (caller tears it down). Raises FixError if nothing verifies."""
    tried = []
    guilty = guilty_commit(rp.repo, rc)
    cands = sorted((r for r in b_results if r.outcome == PASS and r.diff.strip()), key=lambda r: len(r.diff))
    if guilty is None and cands:
        tried.append("rerun fixes: couldn't find the guilty commit between the start state and HEAD")
        cands = []
    for r in cands[:MAX_CANDIDATES]:
        name = f"rerun #{r.run_index}" if r.run_index else "a passing rerun"
        try:
            patch, files = rerun_patch(rp, rc, r.diff, guilty)
        except Exception as e:  # noqa: BLE001
            tried.append(f"{name}: couldn't rebuild its code ({str(e)[:150]})")
            continue
        if not patch:
            tried.append(f"{name}: no code changes outside tests")
            continue
        d, why = verify_at_head(rp, rr, rc, patch)
        if d:
            log(f"pr: {name}'s fix verified at HEAD ({', '.join(files)})")
            return Fix("rerun", patch, files, _verified(rr), r.run_index), d
        tried.append(f"{name}: {why}")
        log(f"pr: {name}'s fix rejected, {why}")
    rev = revert_patch(rc, _head_text(rc.step.file, rp.repo))
    if rev is None:
        tried.append("revert: the guilty edit's text isn't at HEAD any more")
    else:
        d, why = verify_at_head(rp, rr, rc, rev[0])
        if d:
            log("pr: using the revert of the guilty edit, verified at HEAD")
            return Fix("revert", rev[0], [rc.step.file], _verified(rr) + "; no passing rerun's fix verified ("
                       + "; ".join(tried) + ")" if tried else _verified(rr)), d
        tried.append(f"revert: {why}")
    raise FixError("no fix verified at HEAD: " + "; ".join(tried))


# ---------------------------------------------------------------- GitHub

_GH_URL = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def github_remote(repo: str, prefer: str | None = None) -> tuple[str, str]:
    """(remote name, owner/repo) of the GitHub remote: `prefer` if given, else the first github.com one."""
    seen = []
    for ln in _git(repo, "remote", "-v").splitlines():
        parts = ln.split()
        if len(parts) >= 2 and parts[1] not in [u for _, u in seen]:
            seen.append((parts[0], parts[1]))
    for name, url in seen:
        if prefer and name != prefer:
            continue
        m = _GH_URL.search(url)
        if m:
            return name, f"{m[1]}/{m[2]}"
    if prefer:
        raise FixError(f"remote {prefer!r} isn't a github.com remote")
    raise FixError("no github.com remote in this repo (add one, or pass --pr-remote)")


def pr_body(rc: RootCause, report: dict, fix: Fix, rule: str, instructions: str) -> str:
    s = rc.step
    test = (report.get("meta") or {}).get("test", "the test")
    rr = report.get("rerun") or {}
    a, b = rr.get("a") or [], rr.get("b") or []
    lines = ["## What broke", "",
             f"Agent edit #{s.id} to `{s.file}` (session `{s.session_id[:8]}`, {s.agent}) broke `{test}`. "
             "whyagent proved it with test runs: the test passes right before this edit, fails right after it, "
             "and passes again when only this edit is reverted.", ""]
    if s.reasoning:
        lines += ["**The agent's reasoning right before the edit:**",
                  "> " + s.reasoning.strip()[:600].replace("\n", "\n> "), ""]
    lines += ["## The fix", ""]
    if fix.kind == "rerun":
        lines.append(f"The work of a counterfactual rerun (#{fix.run_index}) where the agent redid the task from "
                     "just before the bad edit, with the lesson below. It replaces the session's changes from that "
                     f"edit on, in: {', '.join(f'`{f}`' for f in fix.files)}.")
    else:
        lines.append(f"Reverts the guilty edit in `{s.file}`.")
    lines += ["", f"Verified at this PR's base: {fix.detail}.", "",
              "## The rule", "", f"Added to `{instructions}` so agents in this repo follow it from now on:", "",
              f"> {rule}", ""]
    if rr.get("verdict"):
        p = rr.get("p")
        lines.append(f"Validated by counterfactual reruns ({rr.get('agent')}): without the rule {a.count('PASS')}/"
                     f"{len(a)} passed, with it {b.count('PASS')}/{len(b)}" + (f" (Fisher p = {p:.3f})" if p is not None else "") + ".")
    lines += ["", "---", "Opened by whyagent `--pr`. Review the diff before merging: the tests check what they check."]
    return "\n".join(lines)


def open_pr(rp, rc: RootCause, fix: Fix, d: str, rule: str, report: dict, instructions: str = "AGENTS.md",
            remote: str | None = None, gh=None) -> dict:
    """Commit the verified fix + rule in worktree `d`, push a new branch, open the PR. Returns {url, branch, ...}."""
    repo = rp.repo
    name, slug = github_remote(repo, remote)
    base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if base == "HEAD":
        raise FixError("the analyzed repo is on a detached HEAD; check out the branch the PR should target")
    s = rc.step
    test = (report.get("meta") or {}).get("test", "")
    append_rule(Path(d) / instructions, rule, provenance(test, s.session_id, s.id, s.file))
    _git(d, "add", "--", instructions)
    aid = ((report.get("meta") or {}).get("analysis_id") or "")[:6] or os.urandom(3).hex()
    branch = f"whyagent/fix-{Path(s.file).stem}-{aid}"
    m = re.search(r"\S+::\S+", test)
    tname = m[0] if m else s.file
    title = f"whyagent: fix {tname} (broken by agent edit #{s.id} in {s.file})"
    hooks = tempfile.mkdtemp(prefix="whyagent-nohooks-")  # the analyzed repo's hooks aren't ours to trigger
    try:
        _git(d, "-c", f"core.hooksPath={hooks}", "commit", "-q", "-m", title, "-m",
             f"Rule added to {instructions}: {rule}")
        _git(d, "-c", f"core.hooksPath={hooks}", "push", "-q", name, f"HEAD:refs/heads/{branch}")
    finally:
        os.rmdir(hooks)
    note = None
    remote_base = _git(repo, "ls-remote", "--heads", name, base).split()
    if not remote_base:
        note = f"{name}/{base} doesn't exist; the PR may need a different base"
    elif remote_base[0] != rc.head_state.base_commit and remote_base[0] != _git(repo, "rev-parse", "HEAD").strip():
        note = f"your local {base} isn't what's on {name}; the PR also contains your unpushed commits"
    body = pr_body(rc, report, fix, rule, instructions)
    fd, path = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        args = ["gh", "pr", "create", "--repo", slug, "--base", base, "--head", branch, "--title", title,
                "--body-file", path]
        p = (gh or subprocess.run)(args, cwd=repo, capture_output=True, encoding="utf-8", errors="replace")
    finally:
        os.unlink(path)
    if p.returncode != 0:
        raise FixError(f"pushed {branch} to {name}, but `gh pr create` failed: {(p.stderr or p.stdout).strip()[:300]}")
    url = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else ""
    return {"url": url, "branch": branch, "remote": name, "repo": slug, "base": base, "fix": fix.kind,
            "files": fix.files, "detail": fix.detail, "warning": note}


def make_pr(rp, rr, rc: RootCause, b_results, rule: str, report: dict, instructions: str = "AGENTS.md",
            remote: str | None = None, log=print, gh=None) -> dict:
    """Choose and verify a fix, then open the PR. Raises FixError with the reason when it can't."""
    github_remote(rp.repo, remote)  # fail before spending test runs
    fix, d = choose_fix(rp, rr, rc, b_results, log)
    try:
        return open_pr(rp, rc, fix, d, rule, report, instructions, remote, gh)
    finally:
        rp.teardown(d)
