import subprocess
import sys

from whyagent.replay import Replayer
from whyagent.types import PASS, TARGET_FAIL, State, Step


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=True).stdout.strip()


def test_build_and_run(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("OK = True\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/check.py").write_text("import sys; sys.path.insert(0,'.'); import app; sys.exit(0 if app.OK else 1)\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    steps = [Step(1, "s", "claude-code", "app.py", "edit", "OK = True", "OK = False")]
    rp = Replayer(str(tmp_path), steps, f'"{sys.executable}" tests/check.py')
    assert rp.run(State(base, [], base))[0].outcome == PASS
    assert rp.run(State(base, [1], base))[0].outcome == TARGET_FAIL
    rp.run(State(base, [1], base))  # cached
    assert rp.runs == 2
