"""End-to-end stages 2-6 on a real git repo with real pytest runs (no Entire needed)."""
import subprocess
import sys

from whyagent.adapters.fidelity import Fidelity, apply
from whyagent.pipeline import Options, blind_bisect_runs, find_root_cause
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
    rp = Replayer(str(tmp_path), steps, cmd, target_sig=head_run.signature, sandbox=LocalSandbox())
    rc = find_root_cause(str(tmp_path), steps, Fidelity(matches=True), g, parent, g, rp,
                         Options(line=f"calc/core.py:{decoy_line}"), report, say=lambda *_: None)

    assert rc is not None and rc.step.id == 3 and rc.step.file == "calc/config.py"
    assert report["blame_hint"]["step_ids"] == [4] and report["blame_hint"]["is_cause"] is False
    assert report["pruning"]["verified"] and report["pruning"]["pruned"] == [2, 5]
    assert report["confirm"]["outcome"] == PASS
    assert report["bisect"]["boundary"] == 3
    # "fails right after" has a real probe even when the boundary is the last kept edit
    assert any(p["after_step"] == 3 and p["outcome"] == TARGET_FAIL for p in report["bisect"]["probes"])
    pr = report["pruning"]
    assert pr["runs_with"] is not None and pr["runs_without"] is not None and pr["runs_without_simulated"]
    assert report["bisect"]["edits"] == 4
    assert {s["id"]: s["test"] for s in report["session"]["steps"]}[3] is False
    assert [s.id for s in rc.edits_before] == [1, 2] and rc.before_state.step_ids == [1, 2]
    assert "assert 22 == 20" in rc.failing_output
    assert rp.runs <= 8


def test_blame_hint_right_skips_bisect(tmp_path):
    parent, g, steps, files = make_repo(tmp_path)
    cmd = f'"{sys.executable}" -m pytest -q -p no:cacheprovider tests/test_core.py::test_total'
    sig = Replayer(str(tmp_path), [], cmd).run(State(g, [], g))[0].signature
    line = files["calc/config.py"].splitlines().index("RATE = 11") + 1
    report = Report()
    rp = Replayer(str(tmp_path), steps, cmd, target_sig=sig, sandbox=LocalSandbox())
    rc = find_root_cause(str(tmp_path), steps, Fidelity(matches=True), g, parent, g, rp,
                         Options(line=f"calc/config.py:{line}"), report, say=lambda *_: None)
    assert rc.step.id == 3 and report["blame_hint"]["is_cause"]
    assert "pruning" not in report and rp.runs == 3


def test_parallel_workers_give_the_same_answer(tmp_path):
    parent, g, steps, files = make_repo(tmp_path)
    cmd = f'"{sys.executable}" -m pytest -q -p no:cacheprovider tests/test_core.py::test_total'
    sig = Replayer(str(tmp_path), [], cmd).run(State(g, [], g))[0].signature
    rp = Replayer(str(tmp_path), steps, cmd, target_sig=sig, sandbox=LocalSandbox(), workers=3)
    report = Report()
    rc = find_root_cause(str(tmp_path), steps, Fidelity(matches=True), g, parent, g, rp,
                         Options(workers=3, prune=False), report, say=lambda *_: None)
    assert rc.step.id == 3 and report["confirm"]["outcome"] == PASS


def test_decide_counts_any_target_failure():
    from whyagent.pipeline import decide
    from whyagent.types import OTHER

    assert decide([PASS, PASS, TARGET_FAIL, PASS, PASS]) == TARGET_FAIL  # a 20%-flaky failure still counts
    assert decide([PASS] * 5) == PASS
    assert decide([PASS, OTHER, PASS]) == OTHER
    assert decide([OTHER, TARGET_FAIL]) == TARGET_FAIL


def test_parallel_bisect_on_worker_pool_and_tiger_flag(tmp_path):
    """Same decoy case, with the worker pool (3 sandboxes per bisect round) and a Tiger run log."""
    parent, g, steps, files = make_repo(tmp_path)
    cmd = f'"{sys.executable}" -m pytest -q -p no:cacheprovider tests/test_core.py::test_total'
    sig = Replayer(str(tmp_path), [], cmd).run(State(g, [], g))[0].signature

    class Log:
        def __init__(self):
            self.pruned, self.rows = False, []

        def set_pruned(self, v):
            self.pruned = v

        def log(self, stage, *a, **k):
            self.rows.append(stage)

    log = Log()
    report = Report()
    rp = Replayer(str(tmp_path), steps, cmd, target_sig=sig, sandbox=LocalSandbox(), runlog=log, workers=3)
    rc = find_root_cause(str(tmp_path), steps, Fidelity(matches=True), g, parent, g, rp,
                         Options(workers=3), report, say=lambda *_: None, runlog=log)
    assert rc is not None and rc.step.id == 3 and report["bisect"]["boundary"] == 3
    assert log.pruned is True and "bisect" in log.rows and "prune" in log.rows
    assert len(git(tmp_path, "worktree", "list").splitlines()) == 1  # every temp worktree cleaned up


def test_blind_bisect_runs_counts_states_a_bisect_would_test():
    order = list(range(1, 16))
    assert blind_bisect_runs(order, 7, workers=1) == 4  # binary search over 15 edits: 4 states
    assert blind_bisect_runs(order, 15, workers=1) == 4  # boundary at the end: P(15) is known, not tested
    assert blind_bisect_runs([1], 1, workers=1) == 0      # one edit: P(0) and P(1) already known


def test_precheck_looks_harder_when_head_passes_5_in_a_row(tmp_path, monkeypatch, capsys):
    """A flaky bug can pass the first 5 precheck runs; the precheck must not give up on it."""
    from whyagent.cli import main

    repo, counter = tmp_path / "repo", tmp_path / "count.txt"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("BUGGY = False\n")
    (repo / "tests").mkdir()
    # fails only on its 7th run, and only when the bug is present
    (repo / "tests/check.py").write_text(
        "import sys, pathlib; sys.path.insert(0, '.'); import app\n"
        "if not app.BUGGY: sys.exit(0)\n"  # only HEAD's runs advance the counter
        f"c = pathlib.Path(r'{counter}'); n = int(c.read_text()) + 1 if c.exists() else 1; c.write_text(str(n))\n"
        "sys.exit(1 if n == 7 else 0)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "good")
    good = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("BUGGY = True\n")
    git(repo, "commit", "-qam", "bug (human commit, no Entire trailer)")
    monkeypatch.chdir(repo)
    code = main(["--sandbox", "local", "--workers", "1", "--no-rerun", "--good", good,
                 "--test", f'"{sys.executable}" tests/check.py', "--report", str(tmp_path / "r.html")])
    out = capsys.readouterr().out
    assert "nothing to explain" not in out
    assert "HEAD fails 1/15" in out and "flaky" in out
