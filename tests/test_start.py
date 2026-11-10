"""Guided start: finding the failing tests and the good commit."""
import subprocess
import sys

import pytest

from whyagent import cli, start
from whyagent.replay import LocalSandbox


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
                          check=True).stdout.strip()


def make_repo(tmp_path, ok_values, trailer_from=None):
    """One commit per value of app.OK; commits from index `trailer_from` on carry an Entire trailer."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/check.py").write_text(
        "import sys; sys.path.insert(0,'.'); import app; sys.exit(0 if app.OK else 1)\n")
    shas = []
    for i, ok in enumerate(ok_values):
        (tmp_path / "app.py").write_text(f"OK = {ok}\nN = {i}\n")
        git(tmp_path, "add", "-A")
        msg = f"c{i}" + (f"\n\nEntire-Checkpoint: cp{i}" if trailer_from is not None and i >= trailer_from else "")
        git(tmp_path, "commit", "-qm", msg)
        shas.append(git(tmp_path, "rev-parse", "HEAD"))
    return shas


def test_parse_failed():
    out = ("..F.E\n=== short test summary info ===\n"
           "FAILED tests/test_a.py::test_x - AssertionError: boom\n"
           "ERROR tests/test_b.py::test_y[1-2] - RuntimeError\n"
           "FAILED tests/test_a.py::test_x - AssertionError: boom\n"
           "2 failed, 3 passed\n")
    assert start.parse_failed(out) == ["tests/test_a.py::test_x", "tests/test_b.py::test_y[1-2]"]


def test_candidates_try_before_the_agent_first(tmp_path, monkeypatch):
    shas = make_repo(tmp_path, [True] * 8, trailer_from=5)  # c5..c7 are the agent's
    monkeypatch.chdir(tmp_path)
    got = [c for c, _ in start.candidates(shas[-1])]
    assert got[0] == shas[4]  # parent of the oldest checkpointed commit
    assert got[1:] == [shas[6], shas[5], shas[3], shas[0]]  # HEAD~1, ~2, ~4, then the root; no repeats


def test_candidates_see_past_human_commits_on_top(tmp_path, monkeypatch):
    shas = make_repo(tmp_path, [True] * 5, trailer_from=2)  # c2..c4 agent
    (tmp_path / "app.py").write_text("OK = True\nN = 99\n")
    git(tmp_path, "commit", "-qam", "human fixup")
    monkeypatch.chdir(tmp_path)
    assert start.candidates(git(tmp_path, "rev-parse", "HEAD"))[0][0] == shas[1]


def test_find_good_skips_failing_commits(tmp_path, monkeypatch):
    shas = make_repo(tmp_path, [True, True, False, False, False])  # no trailers: plain back-off
    monkeypatch.chdir(tmp_path)
    test = f'"{sys.executable}" tests/check.py'
    assert start.find_good(test, LocalSandbox(), shas[-1], say=lambda *_: None) == shas[0]  # HEAD~4


def test_find_good_none_when_it_always_fails(tmp_path, monkeypatch):
    shas = make_repo(tmp_path, [False, False])
    monkeypatch.chdir(tmp_path)
    assert start.find_good(f'"{sys.executable}" tests/check.py', LocalSandbox(), shas[-1],
                           say=lambda *_: None) is None


def test_failing_tests_lists_pytest_failures(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_x.py").write_text("def test_ok():\n    pass\n\ndef test_bad():\n    assert 1 == 2\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "c")
    monkeypatch.chdir(tmp_path)
    failed, _ = start.failing_tests(LocalSandbox(), git(tmp_path, "rev-parse", "HEAD"))
    assert failed == ["tests/test_x.py::test_bad"]


def test_no_test_without_a_terminal_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit):
        cli.main([])
    assert "--test is required when not run interactively" in capsys.readouterr().err
