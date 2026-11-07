"""Stage 8: counterfactual reruns. Same agent, same start state P(k-1), with vs without the lesson.

A run succeeds only if (PRD): the target test passes, every test that passed at G still passes,
and the test files are unchanged. Touching tests = TAMPERED (a failure).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..replay import Replayer, is_test_file
from ..types import RootCause

PASS, FAIL, TAMPERED, REGRESSED, OTHER = "PASS", "FAIL", "TAMPERED", "REGRESSED", "OTHER"
_PASSED_RE = re.compile(r"^PASSED (\S+)", re.M)


# ---------------------------------------------------------------- agents

@dataclass
class Agent:
    """How to run one headless agent turn-limited task inside the sandbox.

    `command` reads the task from $WHYAGENT_PROMPT. `image_lines` are extra Dockerfile lines that install the
    agent on top of the HEAD test image. `env` names host env vars to forward (e.g. the API key).
    """
    name: str
    command: str
    image_lines: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    network: str = "bridge"

    def tokens(self, output: str) -> int | None:
        return None

    def error(self, output: str, exit_code: int) -> str | None:
        """Infra failure (auth, quota, crash before doing anything): the run is OTHER, not evidence."""
        return f"agent exited {exit_code} with no output" if exit_code != 0 and not output.strip() else None


def _json_tail(output: str) -> dict | None:
    """The last top-level JSON object in mixed stderr/stdout output."""
    end = output.rfind("}")
    start = output.rfind("\n{", 0, end)
    start = 0 if start < 0 and output.startswith("{") else start + 1
    if start <= 0 and not output.startswith("{"):
        return None
    try:
        return json.loads(output[start:end + 1])
    except ValueError:
        return None


class GeminiCLI(Agent):
    def __init__(self, max_turns: int = 15, model: str | None = None, version: str = "0.67.0"):
        settings = json.dumps({"model": {"maxSessionTurns": max_turns}})
        model = model or os.environ.get("WHYAGENT_RERUN_MODEL")
        super().__init__(
            name="gemini-cli",
            command=('mkdir -p "$HOME/.gemini" && printf "%s" "$WHYAGENT_GEMINI_SETTINGS" > "$HOME/.gemini/settings.json"'
                     ' && gemini -p "$WHYAGENT_PROMPT" --yolo -o json' + (f" -m {model}" if model else "")),
            image_lines=[
                # Node from the official image; Debian-based like python:*-slim, so the binary runs as-is.
                "COPY --from=node:20-slim /usr/local/bin/node /usr/local/bin/node",
                "COPY --from=node:20-slim /usr/local/lib/node_modules /usr/local/lib/node_modules",
                "RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm"
                f" && npm install -g --no-fund --no-audit @google/gemini-cli@{version}",
            ],
            env={"GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""), "GEMINI_CLI_TRUST_WORKSPACE": "true",
                 "WHYAGENT_GEMINI_SETTINGS": settings},
        )

    def error(self, output: str, exit_code: int) -> str | None:
        err = (_json_tail(output) or {}).get("error")
        if err:
            msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
            if "turn" not in msg.lower():  # hitting the turn cap is a real (unfinished) attempt, not infra
                code = err.get("code") if isinstance(err, dict) else None
                return f"Gemini API error{f' {code}' if code else ''}: {msg}"
        return super().error(output, exit_code)

    def tokens(self, output: str) -> int | None:
        data = _json_tail(output)
        if not data:
            return None
        total = 0
        for m in (data.get("stats", {}).get("models", {}) or {}).values():
            t = m.get("tokens", {}) if isinstance(m, dict) else {}
            total += int(t.get("total") or 0)
        return total or None


# ---------------------------------------------------------------- prompt

def task_prompt(rc: RootCause, lesson: str | None) -> str:
    lines = [rc.step.prompt.strip() or "(original task not recorded)", ""]
    if rc.edits_before:
        lines.append("You already made these edits in this session (they're in the working tree):")
        for s in rc.edits_before:
            why = (s.reasoning or "").strip().splitlines()[0][:160] if s.reasoning else "edited"
            lines.append(f"- {s.file}: {why}")
        lines.append("")
    lines.append("Continue the task from here. Don't modify any test files.")
    if lesson:
        lines += ["", f"Lesson from a previous attempt at this task (follow it): {lesson}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- one run

@dataclass
class RerunResult:
    arm: str              # "A" (no lesson) | "B" (lesson)
    outcome: str          # PASS | FAIL | TAMPERED | REGRESSED | OTHER
    detail: str
    duration_ms: int
    tokens: int | None = None


def _test_hashes(root: str, globs) -> dict[str, str]:
    out = {}
    for p in Path(root).rglob("*"):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not rel.startswith(".git") and "__pycache__" not in rel and is_test_file(rel, globs):
            out[rel] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def passed_tests(output: str) -> set[str]:
    """Test IDs reported as PASSED by `pytest -rA`."""
    return set(_PASSED_RE.findall(output))


class Reruns:
    def __init__(self, rp: Replayer, agent: Agent, suite_cmd: str | None = "pytest -q -rA --tb=no",
                 agent_timeout: int = 300, workers: int = 2, runlog=None):
        self.rp, self.agent, self.suite_cmd = rp, agent, suite_cmd
        self.agent_timeout, self.workers, self.runlog = agent_timeout, workers, runlog
        self.image = None
        self._baseline: set[str] | None = None
        self._prepared = False
        self._abort: str | None = None

    def prepare(self, rc: RootCause):
        self._prepared = True
        sb = self.rp.sandbox
        if self.agent.image_lines and hasattr(sb, "extend"):
            self.image = sb.extend(self.agent.name, self.agent.image_lines)
        if self.suite_cmd:  # tests that pass at G must still pass after the rerun
            d = self.rp.build(rc.head_state)
            try:
                self._baseline = passed_tests(sb.exec(d, self.suite_cmd, self.rp.timeout).output)
            finally:
                self.rp.teardown(d)

    def one(self, rc: RootCause, lesson: str | None, arm: str) -> RerunResult:
        if not self._prepared:
            self.prepare(rc)
        if self._abort:
            return RerunResult(arm, OTHER, f"skipped: {self._abort}", 0)
        t0 = time.time()
        d = self.rp.build(rc.before_state)
        sb = self.rp.sandbox
        try:
            before = _test_hashes(d, self.rp.test_globs)
            ro = tuple(sorted({rel.split("/")[0] for rel in before if "/" in rel and (Path(d) / rel.split("/")[0]).is_dir()}))
            env = {**self.agent.env, "WHYAGENT_PROMPT": task_prompt(rc, lesson)}
            ex = sb.exec(d, self.agent.command, self.agent_timeout, env=env, network=self.agent.network,
                         image=self.image, readonly=ro)
            tokens = self.agent.tokens(ex.output)
            ms = lambda: int((time.time() - t0) * 1000)  # noqa: E731
            err = None if ex.timed_out else self.agent.error(ex.output, ex.exit_code)
            if err:
                self._abort = err  # quota/auth problems won't fix themselves mid-analysis
                return RerunResult(arm, OTHER, err, ms(), tokens)
            note = "agent hit the time limit; " if ex.timed_out else ""
            after = _test_hashes(d, self.rp.test_globs)
            if after != before:
                changed = sorted((set(before) ^ set(after)) | {k for k in before if k in after and after[k] != before[k]})
                return RerunResult(arm, TAMPERED, note + "edited tests: " + ", ".join(changed[:5]), ms(), tokens)
            tgt = sb.exec(d, self.rp.test_cmd, self.rp.timeout)
            if tgt.timed_out or tgt.exit_code != 0:
                return RerunResult(arm, FAIL, note + "target test still fails", ms(), tokens)
            if self._baseline:
                lost = self._baseline - passed_tests(sb.exec(d, self.suite_cmd, self.rp.timeout).output)
                if lost:
                    return RerunResult(arm, REGRESSED, note + "broke: " + ", ".join(sorted(lost)[:5]), ms(), tokens)
            return RerunResult(arm, PASS, note + "target passes, no regressions, tests untouched", ms(), tokens)
        finally:
            self.rp.teardown(d)

    def run(self, rc: RootCause, lesson: str, n: int) -> tuple[list[RerunResult], list[RerunResult]]:
        """Both arms, interleaved so rate limits or drift hit both equally."""
        if not self._prepared:
            self.prepare(rc)
        jobs = [(None, "A"), (lesson, "B")] * n
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            results = list(pool.map(lambda j: self.one(rc, *j), jobs))
        for r in results:
            if self.runlog is not None:
                self.runlog.log("rerun", r.arm, r.outcome, r.duration_ms, r.tokens, arm=r.arm)
        return [r for r in results if r.arm == "A"], [r for r in results if r.arm == "B"]


def agent_by_name(name: str) -> Agent:
    if name == "gemini-cli":
        return GeminiCLI()
    raise ValueError(f"unknown rerun agent {name!r} (have: gemini-cli)")
