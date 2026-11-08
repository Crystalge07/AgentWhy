"""Live loop: rule from rerun evidence, approve, write to AGENTS.md, rewind, rerun, verify. Runs on the mock
model with the real mock agent in a LocalSandbox, so no Docker or network is needed."""
import subprocess
import sys

import pytest

from whyagent import entire, tiger
from whyagent.live import LiveLoopError, LiveOptions, run_live
from whyagent.live import mock_data
from whyagent.live.instructions import SECTION, append_rule, has_rule, worktree_files
from whyagent.live.rule import clean_rule
from whyagent.model import MockModel, ModelError, make_model
from whyagent.replay import LocalSandbox, Replayer
from whyagent.types import RootCause, State, Step

RULE = "Never set VERIFY = False in app.py; change OTHER instead (e.g. bump it)."


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "app.py").write_text("# config\nVERIFY = True\nOTHER = 1\n")
    (r / "tests").mkdir()
    (r / "tests/check.py").write_text("import sys; sys.path.insert(0, '.'); import app; sys.exit(0 if app.VERIFY else 1)\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    monkeypatch.chdir(r)
    return r


@pytest.fixture
def no_brain(monkeypatch):
    """Any Brain or Tiger access fails the test."""
    def boom(*a, **k):
        raise AssertionError("the live loop touched Brain/Tiger")
    for name in ("brain_save", "brain_lookup", "_tiger_lessons", "_brain_records"):
        monkeypatch.setattr(entire, name, boom)
    monkeypatch.setattr(tiger, "connect", boom)
    monkeypatch.setenv("TIGER_DATABASE_URL", "postgres://should-not-be-used")


def _scenario(monkeypatch, good_runs=(2,)):
    sc = {"name": "test-verify", "match": lambda rc: rc.step.file == "app.py", "rule": RULE,
          "good_runs": list(good_runs), "good": [["app.py", "OTHER = 1", "OTHER = 2"]]}
    monkeypatch.setattr(mock_data, "SCENARIOS", [sc])


def _rc(base):
    s1 = Step(1, "sess1234abcd", "claude-code", "app.py", "edit", "# config", "# config v2", reasoning="Tidy.",
              prompt="Make the client fail fast")
    s2 = Step(2, "sess1234abcd", "claude-code", "app.py", "edit", "VERIFY = True", "VERIFY = False",
              reasoning="Turn verification off to unblock.", prompt="Make the client fail fast")
    return RootCause(step=s2, collateral=[], before_state=State(base, [1], base), edits_before=[s1],
                     failing_output="AssertionError", head_state=State(base, [1, 2], base))


def _run(repo, opts_kw=None, ask=None):
    base = git(repo, "rev-parse", "HEAD")
    rc = _rc(base)
    test = f'"{sys.executable}" tests/check.py'
    rp = Replayer(str(repo), rc.edits_before + [rc.step], test, sandbox=LocalSandbox())
    opts = LiveOptions(test=test, instructions=str(repo / "AGENTS.md"), runs=3, repo=str(repo),
                       **(opts_kw or {"yes": True}))
    lines = []
    return run_live(rc, rp, MockModel(local=True), opts, out=lines.append, ask=ask), "\n".join(lines)


def test_live_loop_on_mock_end_to_end(repo, monkeypatch, no_brain):
    _scenario(monkeypatch)
    summary, out = _run(repo)
    assert [r["outcome"] for r in summary["counterfactual"]] == ["FAIL", "PASS", "FAIL"]
    assert summary["evidence"]["passed"] == [2] and summary["evidence"]["failed"] == [1, 3]
    assert summary["rule"] == RULE and summary["written"] is True
    assert summary["after"] == {"outcome": "PASS", "detail": "target passes, no regressions, tests untouched",
                                "files": ["app.py"], "label": "rerun passed (unvalidated)"}
    text = (repo / "AGENTS.md").read_text()
    assert text.count(SECTION) == 1 and f"- {RULE}" in text
    assert "<!-- whyagent: test=" in text and "session=sess1234 edit=#2 (app.py)" in text
    for stage in ("1/6 diagnose", "2/6 rule", "3/6 approve", "4/6 rewind", "5/6 rerun", "6/6 verify"):
        assert stage in out
    assert "rerun passed (unvalidated)" in out
    # the original checkout is untouched: only the instructions file is new
    assert git(repo, "status", "--porcelain") == "?? AGENTS.md"
    # a second run doesn't add the rule twice
    summary2, out2 = _run(repo)
    assert summary2["written"] is False and "not added again" in out2
    assert (repo / "AGENTS.md").read_text().count(RULE) == 1


def test_no_contrast_fails_at_rule(repo, monkeypatch, no_brain):
    _scenario(monkeypatch, good_runs=())
    with pytest.raises(LiveLoopError) as e:
        _run(repo)
    assert e.value.stage == "rule" and "none of the 3" in e.value.why
    assert not (repo / "AGENTS.md").exists()


def test_reject_writes_nothing(repo, monkeypatch, no_brain):
    _scenario(monkeypatch)
    with pytest.raises(LiveLoopError) as e:
        _run(repo, {"yes": False}, ask=lambda _: "n")
    assert e.value.stage == "approve" and not (repo / "AGENTS.md").exists()


def test_edit_then_approve(repo, monkeypatch, no_brain):
    _scenario(monkeypatch)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    answers = iter(["e", "Keep VERIFY on in app.py; tune OTHER instead.", "y"])
    summary, _ = _run(repo, {"yes": False}, ask=lambda _: next(answers))
    assert summary["rule"] == "Keep VERIFY on in app.py; tune OTHER instead."
    # the mock agent only follows the rule it finds in AGENTS.md, so the edited rule reached the worktree
    assert summary["after"]["outcome"] == "PASS"


def test_mock_without_canned_response_fails_at_diagnose(repo, monkeypatch, no_brain):
    monkeypatch.setattr(mock_data, "SCENARIOS", [])
    with pytest.raises(LiveLoopError) as e:
        _run(repo)
    assert e.value.stage == "diagnose" and "no canned response" in e.value.why


def test_append_rule_dedupes_and_keeps_section(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("# Project rules\n- Use tabs.")
    assert append_rule(p, "Read config.py first.", "<!-- a -->")
    assert not append_rule(p, "read   CONFIG.py first", "<!-- b -->")
    assert append_rule(p, "Run the tests.", "<!-- c -->")
    text = p.read_text()
    assert text.startswith("# Project rules\n- Use tabs.\n\n" + SECTION)
    assert text.count(SECTION) == 1 and "<!-- b -->" not in text and has_rule(text, "Run the tests")


def test_worktree_files_appends_to_committed_agents_md(repo):
    (repo / "AGENTS.md").write_text("committed rules\n")
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-qm", "agents")
    base = git(repo, "rev-parse", "HEAD")
    demo = repo.parent / "AGENTS.demo.md"
    append_rule(demo, "Rule one.", "<!-- x -->")
    files = worktree_files(demo, str(repo), base, "claude-code")
    assert files["AGENTS.md"].startswith("committed rules\n\n" + SECTION) and "- Rule one." in files["AGENTS.md"]
    assert files["CLAUDE.md"] == "@AGENTS.md\n"
    assert worktree_files(repo / "AGENTS.md", str(repo), base, "gemini-cli") == {"AGENTS.md": "committed rules\n"}


def test_clean_rule_keeps_eg():
    assert clean_rule('Rule: "Add params (e.g. timeout_s) instead"\nextra') == "Add params (e.g. timeout_s) instead."


def test_make_model(monkeypatch):
    monkeypatch.setenv("WHYAGENT_MODEL", "mock")
    assert make_model(None).name == "mock"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert "GEMINI_API_KEY" in make_model("gemini").missing_auth()
    with pytest.raises(ModelError):
        make_model("gpt")
    m = make_model("claude")
    assert m.name == "claude" and m.rerun_agent == "claude-code"
    assert make_model("gemini").rerun_agent == "auto"  # Gemini CLI, Claude Code if Gemini fails
    assert make_model("claude", "stub").rerun_agent == "stub"


def test_cli_flag_checks():
    from whyagent.cli import main
    with pytest.raises(SystemExit):
        main(["--yes", "--test", "x", "--good", "y"])
    with pytest.raises(SystemExit):
        main(["--live", "--trail", "--test", "x", "--good", "y"])
    with pytest.raises(SystemExit):
        main(["--model", "mock", "--test", "x", "--good", "y"])
