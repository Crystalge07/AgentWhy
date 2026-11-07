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


def test_build_preserves_non_ascii_source(tmp_path):
    """Edits are applied as UTF-8 on every OS (Windows defaults to cp1252 and would mangle or crash)."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text('MARK = "✓ café “quoted”"\nOK = True\n', encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    steps = [Step(1, "s", "claude-code", "app.py", "edit", "OK = True", "OK = False")]
    rp = Replayer(str(tmp_path), steps, "true")
    d = rp.build(State(base, [1], base))
    try:
        assert (tmp_path.__class__(d) / "app.py").read_text(encoding="utf-8") == 'MARK = "✓ café “quoted”"\nOK = False\n'
    finally:
        rp.teardown(d)


import pytest

from whyagent.replay import DockerSandbox, docker_available
from whyagent.types import OTHER

docker_ok, docker_why = docker_available()
needs_docker = pytest.mark.skipif(not docker_ok, reason=docker_why)


def _repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("OK = True\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/check.py").write_text(
        "import sys, urllib.request\nsys.path.insert(0, '.')\nimport app\n"
        "if '--net' in sys.argv:\n"
        "    try: urllib.request.urlopen('http://example.com', timeout=3); sys.exit(0)\n"
        "    except Exception: sys.exit(3)\n"
        "sys.exit(0 if app.OK else 1)\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return git(tmp_path, "rev-parse", "HEAD")


@needs_docker
def test_docker_sandbox(tmp_path):
    base = _repo(tmp_path)
    sb = DockerSandbox(str(tmp_path), base, setup="true", quiet=True)
    steps = [Step(1, "s", "claude-code", "app.py", "edit", "OK = True", "OK = False")]
    rp = Replayer(str(tmp_path), steps, "python tests/check.py", sandbox=sb)
    assert rp.run(State(base, [], base))[0].outcome == PASS
    assert rp.run(State(base, [1], base))[0].outcome == TARGET_FAIL
    # network is off inside the container
    assert Replayer(str(tmp_path), [], "python tests/check.py --net", sandbox=sb).run(State(base, [], base))[0].exit_code == 3
    # timeouts count as OTHER and the container is removed
    r = Replayer(str(tmp_path), [], "sleep 30", sandbox=sb, timeout=2).run(State(base, [], base))[0]
    assert r.outcome == OTHER and r.signature == "timeout"
    left = subprocess.run(["docker", "ps", "-aq", "--filter", "name=whyagent-run-"], capture_output=True,
                          encoding="utf-8").stdout.strip()
    assert left == ""
