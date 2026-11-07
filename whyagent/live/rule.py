"""Live loop, rule stage: contrast the no-rule reruns that passed with the ones that didn't, and turn the
difference into one rule. The evidence (which runs passed, what they changed) is computed here from the
runs' diffs, not by the model; the model only phrases the rule."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..rerun.harness import PASS, RerunResult
from ..rerun.lesson import _clip
from ..types import RootCause

PROMPT = """A coding agent made an edit that broke a test. We proved which edit by experiment. Then we reran
the agent {n} times from just before that edit, with no extra instructions. Some reruns passed the test and
some didn't. Write a rule for the agent's instructions file (AGENTS.md) that makes it do what the passing
runs did instead of what the failing runs did.

Task the agent was given:
{prompt}

The guilty edit, in {file}:
--- before
{old}
+++ after
{new}

Failing test output after the guilty edit:
{failing}

Reruns that PASSED ({n_pass}), with their diffs:
{passed}

Reruns that did NOT pass ({n_fail}), with their diffs:
{failed}

Rules for the rule:
- One sentence, imperative mood, under 25 words.
- Say what to avoid and what to do instead, naming the concrete file, setting or API when it helps.
- It must explain the difference between the passing and failing runs, not restate the task.
- No preamble, quotes or markdown. Output only the sentence."""


@dataclass
class Group:
    files: tuple[str, ...]
    runs: list[RerunResult] = field(default_factory=list)
    sample: list[str] = field(default_factory=list)  # a few changed lines from the first run

    @property
    def label(self) -> str:
        ids = ", ".join(str(r.run_index) for r in self.runs)
        return f"run{'s' if len(self.runs) > 1 else ''} {ids}"


@dataclass
class Evidence:
    passed: list[RerunResult]
    failed: list[RerunResult]   # FAIL, TAMPERED, REGRESSED: the agent ran but didn't fix it
    pass_groups: list[Group]
    fail_groups: list[Group]
    only_pass: list[str]        # files only passing runs changed
    only_fail: list[str]        # files only failing runs changed

    def lines(self) -> list[str]:
        out = [f"{len(self.passed)} of {len(self.passed) + len(self.failed)} reruns passed without a rule"]
        for title, groups in (("passed", self.pass_groups), ("didn't pass", self.fail_groups)):
            for g in groups:
                outcomes = sorted({r.outcome for r in g.runs})
                tag = "" if outcomes == [PASS] else f" ({'/'.join(o.lower() for o in outcomes)})"
                out.append(f"{title}: {g.label}{tag} changed {', '.join(g.files) or 'nothing'}")
                out += [f"    {ln}" for ln in g.sample]
        if self.only_pass or self.only_fail:
            out.append("difference: passing runs changed " + (", ".join(self.only_pass) or "no other files")
                       + "; failing runs changed " + (", ".join(self.only_fail) or "no other files"))
        return out

    def as_dict(self) -> dict:
        return {"passed": [r.run_index for r in self.passed], "failed": [r.run_index for r in self.failed],
                "only_pass": self.only_pass, "only_fail": self.only_fail, "summary": self.lines()}


_FILE = re.compile(r"^diff --git a/(\S+) b/", re.M)


def files_changed(diff: str) -> tuple[str, ...]:
    return tuple(sorted(set(_FILE.findall(diff))))


def changed_lines(diff: str, k: int = 4) -> list[str]:
    out = [ln.rstrip() for ln in diff.splitlines()
           if ln[:1] in "+-" and not ln.startswith(("+++", "---")) and ln[1:].strip()]
    return [ln if len(ln) <= 100 else ln[:99] + "…" for ln in out[:k]]


def _groups(runs: list[RerunResult]) -> list[Group]:
    by: dict[tuple[str, ...], Group] = {}
    for r in runs:
        g = by.setdefault(files_changed(r.diff), Group(files_changed(r.diff)))
        g.runs.append(r)
        if not g.sample:
            g.sample = changed_lines(r.diff)
    return list(by.values())


def evidence(results: list[RerunResult]) -> Evidence:
    """Split valid reruns into passed / didn't pass. OTHER (the agent never really ran) is left out."""
    passed = [r for r in results if r.outcome == PASS]
    failed = [r for r in results if r.outcome not in (PASS, "OTHER")]
    fp = set().union(*[files_changed(r.diff) for r in passed]) if passed else set()
    ff = set().union(*[files_changed(r.diff) for r in failed]) if failed else set()
    return Evidence(passed, failed, _groups(passed), _groups(failed), sorted(fp - ff), sorted(ff - fp))


def _runs_text(runs: list[RerunResult]) -> str:
    if not runs:
        return "(none)"
    return "\n\n".join(f"Run {r.run_index} ({r.outcome}):\n{_clip(r.diff, 1500) or '(no changes)'}" for r in runs[:3])


def build_prompt(rc: RootCause, ev: Evidence) -> str:
    s = rc.step
    return PROMPT.format(
        n=len(ev.passed) + len(ev.failed), prompt=_clip(s.prompt, 800) or "(not recorded)", file=s.file,
        old=_clip(s.old_text, 1200) or "(new file)", new=_clip(s.new_text, 1200),
        failing=_clip(rc.failing_output, 1200) or "(not available)",
        n_pass=len(ev.passed), passed=_runs_text(ev.passed), n_fail=len(ev.failed), failed=_runs_text(ev.failed))


def clean_rule(text: str) -> str:
    """First non-empty line, without a "Rule:" prefix, bullets, quotes or markdown. Unlike Stage 7's lesson
    cleaner it doesn't cut at the first period, so "e.g." inside a rule survives."""
    line = next((ln for ln in (text or "").strip().splitlines() if ln.strip()), "")
    line = re.sub(r"^\s*(?:[-*]\s+)?(?:rule\s*:)?\s*", "", line, flags=re.I).strip().strip('"“”\'*` ')
    return line if not line or line.endswith((".", "!")) else line + "."


def write_rule(rc: RootCause, ev: Evidence, generate) -> str:
    return clean_rule(generate(build_prompt(rc, ev)))
