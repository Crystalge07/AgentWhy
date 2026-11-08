import subprocess

from whyagent import entire
from whyagent.rules import rules_markdown

RECS = [{"text": "Never lower shared config defaults.", "repo": "agentwhy/payments-demo", "saved_at": "1"},
        {"text": "Keep a method's old default; add the new behaviour under a new name.", "repo": "a/b", "saved_at": "2"},
        {"text": "never lower shared config defaults", "saved_at": "3"}]  # same rule again, no repo


def test_rules_newest_first_deduped_and_capped(tmp_path):
    md = rules_markdown(cwd=str(tmp_path), records=RECS)
    assert md.startswith("## Rules from agentwhy")
    lines = [ln for ln in md.splitlines() if ln.startswith("- ")]
    assert lines == ["- never lower shared config defaults",
                     "- Keep a method's old default; add the new behaviour under a new name. _(from a/b)_"]
    assert len([ln for ln in rules_markdown(cwd=str(tmp_path), records=RECS, limit=1).splitlines()
                if ln.startswith("- ")]) == 1


def test_rules_skip_what_agents_md_already_has(tmp_path):
    (tmp_path / "AGENTS.md").write_text("## Rules from agentwhy\n\n- Never lower shared config defaults.\n")
    md = rules_markdown(cwd=str(tmp_path), records=RECS)
    assert "config defaults" not in md and "old default" in md
    assert rules_markdown(cwd=str(tmp_path), records=[]) == ""


def test_repo_name(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    assert entire.repo_name(str(tmp_path)) == tmp_path.name
    git("remote", "add", "github", "https://github.com/Crystalge07/whyagent-demo.git")
    assert entire.repo_name(str(tmp_path)) == "Crystalge07/whyagent-demo"
    git("remote", "add", "origin", "entire://aws-us-east-2.entire.io/et/agentwhy/whyagent-demo")
    assert entire.repo_name(str(tmp_path)) == "agentwhy/whyagent-demo"


def test_install_and_uninstall_hook(tmp_path):
    import json

    from whyagent.rules import claude_settings_path, install_hook, uninstall_hook

    path = claude_settings_path(str(tmp_path))
    path.write_text(json.dumps({"model": "opus", "hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    assert install_hook(path, '"/py" -m whyagent.cli --rules') == "added"
    assert install_hook(path, '"/py" -m whyagent.cli --rules') == "unchanged"
    assert install_hook(path, '"/other/py" -m whyagent.cli --rules') == "updated"
    d = json.loads(path.read_text())
    cmds = [h["command"] for g in d["hooks"]["SessionStart"] for h in g["hooks"]]
    assert d["model"] == "opus" and cmds == ["echo hi", '"/other/py" -m whyagent.cli --rules']
    assert uninstall_hook(path) and not uninstall_hook(path)
    assert json.loads(path.read_text()) == {"model": "opus", "hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "echo hi"}]}]}}
    fresh = claude_settings_path(str(tmp_path / "new"))
    assert install_hook(fresh) == "added" and "whyagent.cli --rules" in fresh.read_text()
    assert uninstall_hook(fresh) and json.loads(fresh.read_text()) == {}
