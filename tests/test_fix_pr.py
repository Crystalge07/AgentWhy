import subprocess
import sys
import types

import pytest

from whyagent import fix_pr
from whyagent.replay import LocalSandbox, Replayer
from whyagent.rerun import Agent, Reruns
from whyagent.types import RootCause, State, Step

PY = f'"{sys.executable}"'


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
                          check=True).stdout.strip()


def _step(i, old, new):
    return Step(i, "sess1234", "claude-code", "app.py", "edit", old, new, prompt="Make the client stricter",
                reasoning="Turning verification off to unblock.")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "app.py").write_text("# config\nVERIFY = True\nOTHER = 1\n")
    (r / "tests").mkdir()
    (r / "tests/check.py").write_text("import sys; sys.path.insert(0, '.'); import app; sys.exit(0 if app.VERIFY else 1)\n")
    (r / "tests/suite.py").write_text(
        "import sys; sys.path.insert(0, '.'); import app\n"
        "if app.VERIFY: print('PASSED tests/check.py::verify')\n"
        "if app.OTHER == 1: print('PASSED tests/suite.py::other')\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    base = git(r, "rev-parse", "HEAD")
    (r / "app.py").write_text("# config v2\nVERIFY = False\nOTHER = 1\n")  # the agent session: edits 1 and 2
    git(r, "commit", "-qam", "session")
    (r / "b.py").write_text("LATER = 1\n")  # a later commit, so HEAD isn't the guilty commit
    git(r, "add", "-A")
    git(r, "commit", "-qm", "later")
    head = git(r, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(r, "remote", "add", "origin", str(remote))
    git(r, "push", "-q", "origin", "main")
    edits = [_step(1, "# config", "# config v2")]
    rc = RootCause(step=_step(2, "VERIFY = True", "VERIFY = False"), collateral=[],
                   before_state=State(base, [1], head), edits_before=edits, failing_output="AssertionError",
                   head_state=State(head, [], head))
    return r, remote, rc


def _reruns(r, rc, tmp_path):
    # Without the lesson the agent repeats the mistake; with it, it adds a stricter option instead.
    script = tmp_path / "agent.py"
    script.write_text(
        "import os\n"
        "s = open('app.py').read()\n"
        "if 'Lesson' in os.environ['WHYAGENT_PROMPT']:\n"
        "    s += 'STRICT = True\\n'\n"
        "else:\n"
        "    s = s.replace('VERIFY = True', 'VERIFY = False')\n"
        "open('app.py', 'w', newline='\\n').write(s)\n")
    rp = Replayer(str(r), rc.edits_before + [rc.step], f"{PY} tests/check.py", sandbox=LocalSandbox())
    rr = Reruns(rp, Agent("fake", f'{PY} "{script}"', network="none"), suite_cmd=f"{PY} tests/suite.py")
    rr.capture_diff = True
    a, b = rr.run(rc, "Never disable TLS verification.", 2)
    return rp, rr, a, b


def _gh(seen):
    def run(args, **kw):
        seen["args"] = args
        seen["body"] = open(args[args.index("--body-file") + 1], encoding="utf-8").read()
        return types.SimpleNamespace(returncode=0, stdout="https://github.com/o/r/pull/7\n", stderr="")
    return run


REPORT = {"meta": {"test": f"{PY} tests/check.py", "analysis_id": "abc123def"},
          "rerun": {"agent": "fake", "a": ["FAIL", "FAIL"], "b": ["PASS", "PASS"], "p": 0.17, "verdict": "VALIDATED"}}


def test_pr_uses_the_rerun_fix_verified_at_head(repo, tmp_path, monkeypatch):
    r, remote, rc = repo
    rp, rr, a, b = _reruns(r, rc, tmp_path)
    assert [x.outcome for x in a] == ["FAIL", "FAIL"] and [x.outcome for x in b] == ["PASS", "PASS"]
    monkeypatch.setattr(fix_pr, "github_remote", lambda repo, prefer=None: ("origin", "o/r"))
    head_before = git(r, "rev-parse", "HEAD")
    seen = {}
    pr = fix_pr.make_pr(rp, rr, rc, b, "Never disable TLS verification.", REPORT, gh=_gh(seen), log=lambda m: None)

    assert pr["url"] == "https://github.com/o/r/pull/7" and pr["fix"] == "rerun" and pr["files"] == ["app.py"]
    br = pr["branch"]
    assert br == "whyagent/fix-app-abc123"
    # the rerun's work replaces the session's from the guilty edit on; the later commit is kept
    assert git(remote, "show", f"{br}:app.py") == "# config v2\nVERIFY = True\nOTHER = 1\nSTRICT = True"
    assert git(remote, "show", f"{br}:b.py") == "LATER = 1"
    agents = git(remote, "show", f"{br}:AGENTS.md")
    assert "- Never disable TLS verification." in agents and "edit=#2 (app.py)" in agents
    assert git(remote, "rev-parse", f"{br}^") == head_before
    assert set(git(remote, "diff", "--name-only", f"{br}^", br).split()) == {"app.py", "AGENTS.md"}
    a_ = seen["args"]
    assert a_[a_.index("--repo") + 1] == "o/r" and a_[a_.index("--base") + 1] == "main"
    assert a_[a_.index("--head") + 1] == br
    for want in ("Agent edit #2", "Turning verification off", "counterfactual rerun", "with it 2/2", "AGENTS.md"):
        assert want in seen["body"], want
    # the user's checkout is untouched
    assert git(r, "rev-parse", "HEAD") == head_before and git(r, "status", "--porcelain") == ""
    assert git(r, "worktree", "list").count("\n") == 0


def test_falls_back_to_the_revert(repo, tmp_path):
    r, remote, rc = repo
    rp = Replayer(str(r), rc.edits_before + [rc.step], f"{PY} tests/check.py", sandbox=LocalSandbox())
    rr = Reruns(rp, Agent("fake", "true", network="none"), suite_cmd=f"{PY} tests/suite.py")
    rr.prepare(rc)
    fix, d = fix_pr.choose_fix(rp, rr, rc, [], log=lambda m: None)
    try:
        assert fix.kind == "revert" and "+VERIFY = True" in fix.patch
    finally:
        rp.teardown(d)


def test_nothing_verifies(repo):
    r, remote, rc = repo
    rp = Replayer(str(r), rc.edits_before + [rc.step], f"{PY} -c \"raise SystemExit(1)\"", sandbox=LocalSandbox())
    rr = Reruns(rp, Agent("fake", "true", network="none"), suite_cmd=None)
    with pytest.raises(fix_pr.FixError, match="revert: target test still fails"):
        fix_pr.choose_fix(rp, rr, rc, [], log=lambda m: None)


def test_github_remote(repo):
    r, _, _ = repo
    git(r, "remote", "add", "github", "git@github.com:Crystalge07/AgentWhy.git")
    git(r, "remote", "add", "upstream", "https://github.com/a/b")
    assert fix_pr.github_remote(str(r)) == ("github", "Crystalge07/AgentWhy")  # origin isn't GitHub
    assert fix_pr.github_remote(str(r), "upstream") == ("upstream", "a/b")
    with pytest.raises(fix_pr.FixError):
        fix_pr.github_remote(str(r), "origin")
