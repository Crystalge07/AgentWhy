"""Build States into worktrees and run the test against them."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..types import OTHER, PASS, TARGET_FAIL, RunResult, State, Step

DEFAULT_TEST_GLOBS = ["tests/*", "test_*.py", "*/test_*.py", "conftest.py", "*/conftest.py"]
_NOISE = re.compile(r"0x[0-9a-f]+|\d+(\.\d+)?|(/[\w.\-]+)+|\d{4}-\d\d-\d\dT[\d:.]+", re.I)


def signature(output: str) -> str:
    tail = "\n".join(output.strip().splitlines()[-20:])
    return hashlib.sha1(_NOISE.sub("#", tail).encode()).hexdigest()[:12]


def is_test_file(path: str, globs=DEFAULT_TEST_GLOBS) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


def _git(*args, cwd="."):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=True).stdout


class Replayer:
    def __init__(self, repo: str, steps: list[Step], test_cmd: str, target_sig: str | None = None,
                 timeout: int = 120, test_globs=DEFAULT_TEST_GLOBS):
        self.repo = os.path.abspath(repo)
        self.steps = {s.id: s for s in steps}
        self.test_cmd = test_cmd
        self.target_sig = target_sig
        self.timeout = timeout
        self.test_globs = test_globs
        self.cache: dict[str, list[RunResult]] = {}
        self.runs = 0

    def build(self, state: State) -> str:
        d = tempfile.mkdtemp(prefix="whyagent-")
        _git("worktree", "add", "--detach", d, state.base_commit, cwd=self.repo)
        for sid in state.step_ids:
            s = self.steps[sid]
            if is_test_file(s.file, self.test_globs):
                continue
            p = Path(d) / os.path.relpath(s.file, self.repo) if os.path.isabs(s.file) else Path(d) / s.file
            p.parent.mkdir(parents=True, exist_ok=True)
            text = p.read_text() if p.exists() else ""
            if s.old_text == "":
                text = text + s.new_text if text else s.new_text
            elif text.count(s.old_text) >= 1:
                text = text.replace(s.old_text, s.new_text, 1)
            else:
                raise ValueError(f"step {sid}: old_text not found in {s.file}")
            p.write_text(text)
        # pin test files from pinned_tests_from
        for f in _git("ls-tree", "-r", "--name-only", state.pinned_tests_from, cwd=self.repo).splitlines():
            if is_test_file(f, self.test_globs):
                dst = Path(d) / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(subprocess.run(["git", "show", f"{state.pinned_tests_from}:{f}"],
                                               cwd=self.repo, capture_output=True, check=True).stdout)
        return d

    def teardown(self, d: str):
        subprocess.run(["git", "worktree", "remove", "--force", d], cwd=self.repo, capture_output=True)
        shutil.rmtree(d, ignore_errors=True)

    def _once(self, d: str) -> RunResult:
        t0 = time.time()
        self.runs += 1
        try:
            p = subprocess.run(self.test_cmd, shell=True, cwd=d, capture_output=True, encoding="utf-8", errors="replace",
                               timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return RunResult(OTHER, "timeout", -1, int((time.time() - t0) * 1000))
        sig = signature(p.stdout + p.stderr)
        ms = int((time.time() - t0) * 1000)
        if p.returncode == 0:
            return RunResult(PASS, sig, 0, ms)
        if self.target_sig is None or sig == self.target_sig:
            return RunResult(TARGET_FAIL, sig, p.returncode, ms)
        return RunResult(OTHER, sig, p.returncode, ms)

    def run(self, state: State, n: int = 1) -> list[RunResult]:
        k = state.key()
        if k in self.cache and len(self.cache[k]) >= n:
            return self.cache[k][:n]
        d = self.build(state)
        try:
            res = [self._once(d) for _ in range(n)]
        finally:
            self.teardown(d)
        self.cache[k] = res
        return res

    def outcome(self, state: State, n: int = 1) -> str:
        """Majority outcome across n runs."""
        res = [r.outcome for r in self.run(state, n)]
        return max(set(res), key=res.count)
