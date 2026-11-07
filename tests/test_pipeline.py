"""End-to-end stages 2-6 on a real git repo with real pytest runs (no Entire needed)."""
import subprocess
import sys

from whyagent.adapters.fidelity import Fidelity, apply
from whyagent.pipeline import Options, find_root_cause
from whyagent.replay import LocalSandbox, Replayer
from whyagent.report import Report
from whyagent.types import PASS, TARGET_FAIL, State, Step

BASE = {
    "calc/__init__.py": "",
    "calc/config.py": "RATE = 10\nLIMIT = 100\n",
    "calc/core.py": "from . import config\n\n\ndef total(x):\n    return x * config.RATE\n\n\ndef cap(x):\n"
                    "    return min(x, config.LIMIT)\n",
    "calc/report.py": "def title():\n    return 'Report'\n",
    "tests/test_core.py": "import sys\nsys.path.insert(0, '.')\nfrom calc.core import total\n\n\n"
                          "def test_total():\n    assert total(2) == 20\n",
}
EDITS = [  # (file, old, new); edit 3 is the real cause, edit 4 is the decoy the traceback points at
    ("calc/core.py", "def total(x):", "# totals\ndef total(x):"),
    ("calc/report.py", "'Report'", "'Summary'"),
    ("calc/config.py", "RATE = 10", "RATE = 11"),
    ("calc/core.py", "    return x * config.RATE", "    result = x * config.RATE\n    return result"),
    ("calc/report.py", "def title():", "def title() -> str:"),
    ("calc/core.py", "    return min(x, config.LIMIT)", "    return max(0, min(x, config.LIMIT))"),
]


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, encoding="utf-8", check=True).stdout.strip()


def make_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    for f, text in BASE.items():
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_bytes(text.encode())
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    parent = git(tmp_path, "rev-parse", "HEAD")
    steps = [Step(i, "sess1", "claude-code", f, "edit", o, n, reasoning=f"edit {i}", prompt="tidy up")
             for i, (f, o, n) in enumerate(EDITS, 1)]
    files = dict(BASE)
    for s in steps:
        files[s.file] = apply(files[s.file], s)
    for f, text in files.items():
        (tmp_path / f).write_bytes(text.encode())
    git(tmp_path, "commit", "-qam", "agent work")
    return parent, git(tmp_path, "rev-parse", "HEAD"), steps, files


def test_finds_real_cause_when_blame_points_at_a_decoy(tmp_path):
    parent, g, steps, files = make_repo(tmp_path)
    cmd = f'"{sys.executable}" -m pytest -q -p no:cacheprovider tests/test_core.py::test_total'
    head_run = Replayer(str(tmp_path), [], cmd).run(State(g, [], g))[0]
    assert head_run.outcome == TARGET_FAIL
    decoy_line = files["calc/core.py"].splitlines().index("    return result") + 1
    report = Report()
    rc = find_root_cause(str(tmp_path), steps, Fidelity(matches=True), g, parent, g, cmd, head_run.signature,
                         LocalSandbox(), Options(line=f"calc/core.py:{decoy_line}"), report, say=lambda *_: None)

    assert rc is not None and rc.step.id == 3 and rc.step.file == "calc/config.py"
    assert report["blame_hint"]["step_ids"] == [4] and report["blame_hint"]["is_cause"] is False
    assert report["pruning"]["verified"] and report["pruning"]["pruned"] == [2, 5]
    assert report["confirm"]["outcome"] == PASS
    assert report["bisect"]["boundary"] == 3
    assert [s.id for s in rc.edits_before] == [1, 2] and rc.before_state.step_ids == [1, 2]
    assert "assert 22 == 20" in rc.failing_output
    assert report["runs_total"] <= 8


def test_blame_hint_right_skips_bisect(tmp_path):
    parent, g, steps, files = make_repo(tmp_path)
    cmd = f'"{sys.executable}" -m pytest -q -p no:cacheprovider tests/test_core.py::test_total'
    sig = Replayer(str(tmp_path), [], cmd).run(State(g, [], g))[0].signature
    line = files["calc/config.py"].splitlines().index("RATE = 11") + 1
    report = Report()
    rc = find_root_cause(str(tmp_path), steps, Fidelity(matches=True), g, parent, g, cmd, sig,
                         LocalSandbox(), Options(line=f"calc/config.py:{line}"), report, say=lambda *_: None)
    assert rc.step.id == 3 and report["blame_hint"]["is_cause"]
    assert "pruning" not in report and report["runs_total"] == 3


def test_decide_counts_any_target_failure():
    from whyagent.pipeline import decide
    from whyagent.types import OTHER

    assert decide([PASS, PASS, TARGET_FAIL, PASS, PASS]) == TARGET_FAIL  # a 20%-flaky failure still counts
    assert decide([PASS] * 5) == PASS
    assert decide([PASS, OTHER, PASS]) == OTHER
    assert decide([OTHER, TARGET_FAIL]) == TARGET_FAIL
