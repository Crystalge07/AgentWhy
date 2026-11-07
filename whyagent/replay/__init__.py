"""Build States into worktrees and run the test against them."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..types import OTHER, PASS, TARGET_FAIL, RunResult, State, Step
from .sandbox import DockerSandbox, LocalSandbox, SandboxError, docker_available  # noqa: F401

DEFAULT_TEST_GLOBS = ["tests/*", "test_*.py", "*/test_*.py", "conftest.py", "*/conftest.py"]
_NOISE = re.compile(r"0x[0-9a-f]+|\d+(\.\d+)?|(/[\w.\-]+)+|\d{4}-\d\d-\d\dT[\d:.]+", re.I)


def signature(output: str) -> str:
    tail = "\n".join(output.strip().splitlines()[-20:])
    return hashlib.sha1(_NOISE.sub("#", tail).encode()).hexdigest()[:12]


def is_test_file(path: str, globs=DEFAULT_TEST_GLOBS) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


def vote(outcomes: list[str]) -> str:
    """One verdict from n runs of the same state.

    Any TARGET_FAIL means the bug is present: a failure is evidence, while passes never prove a flaky
    bug absent. A majority vote would call the recorded cache bug (fails ~28% of runs) PASS almost
    every time. PASS needs every run to pass; anything else (crashes, other failures) is OTHER.
    """
    if TARGET_FAIL in outcomes:
        return TARGET_FAIL
    return PASS if outcomes and all(o == PASS for o in outcomes) else OTHER


def _git(*args, cwd="."):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=True).stdout


class Replayer:
    def __init__(self, repo: str, steps: list[Step], test_cmd: str, target_sig: str | None = None,
                 timeout: int = 120, test_globs=DEFAULT_TEST_GLOBS, sandbox=None, runlog=None, workers: int = 1):
        self.repo = os.path.abspath(repo)
        self.steps = {s.id: s for s in steps}
        self.test_cmd = test_cmd
        self.target_sig = target_sig
        self.timeout = timeout
        self.test_globs = test_globs
        self.cache: dict[str, list[RunResult]] = {}
        self.runs = 0
        self.sandbox = sandbox or LocalSandbox()
        self.runlog = runlog  # tiger.RunLog or None
        self.stage = "run"  # set by the caller; tags Tiger rows
        self.workers = max(1, workers)  # sandboxes run in parallel by run_many
        self._git_lock = threading.Lock()  # concurrent `git worktree add/remove` race on .git/worktrees
        self._lock = threading.Lock()
        self.sandbox.prepare()

    def build(self, state: State) -> str:
        d = tempfile.mkdtemp(prefix="whyagent-")
        with self._git_lock:
            # byte-exact checkout: agent edits use LF, so a CRLF checkout (autocrlf=true) would break them
            _git("-c", "core.autocrlf=false", "-c", "core.eol=lf", "worktree", "add", "--detach", d,
                 state.base_commit, cwd=self.repo)
        for sid in state.step_ids:
            s = self.steps[sid]
            if is_test_file(s.file, self.test_globs):
                continue
            p = Path(d) / os.path.relpath(s.file, self.repo) if os.path.isabs(s.file) else Path(d) / s.file
            p.parent.mkdir(parents=True, exist_ok=True)
            text = p.read_bytes().decode("utf-8") if p.exists() else ""  # bytes: no newline translation
            if s.old_text == "":
                text = text + s.new_text if text else s.new_text
            elif text.count(s.old_text) >= 1:
                text = text.replace(s.old_text, s.new_text, 1)
            else:
                raise ValueError(f"step {sid}: old_text not found in {s.file}")
            p.write_bytes(text.encode("utf-8"))
        # pin test files from pinned_tests_from
        for f in _git("ls-tree", "-r", "--name-only", state.pinned_tests_from, cwd=self.repo).splitlines():
            if is_test_file(f, self.test_globs):
                dst = Path(d) / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(subprocess.run(["git", "show", f"{state.pinned_tests_from}:{f}"],
                                               cwd=self.repo, capture_output=True, check=True).stdout)
        return d

    def teardown(self, d: str):
        with self._git_lock:
            subprocess.run(["git", "worktree", "remove", "--force", d], cwd=self.repo, capture_output=True)
        shutil.rmtree(d, ignore_errors=True)

    def _once(self, d: str) -> RunResult:
        t0 = time.time()
        with self._lock:
            self.runs += 1
        e = self.sandbox.exec(d, self.test_cmd, self.timeout)
        ms = int((time.time() - t0) * 1000)
        tail = (e.output or "")[-4000:]
        if e.timed_out:
            return RunResult(OTHER, "timeout", -1, ms, output=tail)
        sig = signature(e.output)
        if e.exit_code == 0:
            return RunResult(PASS, sig, 0, ms, output=tail)
        if self.target_sig is None or sig == self.target_sig:
            return RunResult(TARGET_FAIL, sig, e.exit_code, ms, output=tail)
        return RunResult(OTHER, sig, e.exit_code, ms, output=tail)

    def run(self, state: State, n: int = 1) -> list[RunResult]:
        k = state.key()
        with self._lock:
            hit = self.cache.get(k)
        if hit is not None and len(hit) >= n:
            res = hit[:n]
            self._log(k, res, cached=True)
            return res
        d = self.build(state)
        try:
            res = [self._once(d) for _ in range(n)]
        finally:
            self.teardown(d)
        with self._lock:
            self.cache[k] = res
        self._log(k, res)
        return res

    def run_many(self, states: list[State], n: int = 1) -> list[list[RunResult]]:
        """Test several states at once on up to `workers` sandboxes (one bisect round, say). Order is kept;
        duplicate states run once. A state's n repeats stay sequential in one worktree."""
        uniq = list({s.key(): s for s in states}.values())
        with ThreadPoolExecutor(max_workers=min(self.workers, len(uniq)) or 1) as pool:
            done = dict(zip([s.key() for s in uniq], pool.map(lambda s: self.run(s, n), uniq)))
        return [done[s.key()] for s in states]

    def outcomes(self, states: list[State], n: int = 1) -> list[str]:
        return [vote([r.outcome for r in res]) for res in self.run_many(states, n)]

    def _log(self, key: str, res: list[RunResult], cached: bool = False):
        if self.runlog is not None:
            for r in res:
                self.runlog.log(self.stage, key, r.outcome, 0 if cached else r.duration_ms, r.tokens, cached=cached)

    def outcome(self, state: State, n: int = 1) -> str:
        """One outcome from n runs (see vote)."""
        return vote([r.outcome for r in self.run(state, n)])
