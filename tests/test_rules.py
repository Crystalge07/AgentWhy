import subprocess

from whyagent import entire
from whyagent.rules import rules_markdown

RECS = [{"text": "Never lower shared config defaults.", "repo": "agentwhy/payments-demo", "saved_at": "1"},
        {"text": "Keep a method's old default; add the new behaviour under a new name.", "repo": "a/b", "saved_at": "2"},
        {"text": "never lower shared config defaults", "saved_at": "3"}]  # same rule again, no repo


def test_rules_newest_first_deduped_and_capped(tmp_path):
    md = rules_markdown(cwd=str(tmp_path), records=RECS)
    assert md.startswith("## Rules from whyagent")
    lines = [ln for ln in md.splitlines() if ln.startswith("- ")]
    assert lines == ["- never lower shared config defaults",
                     "- Keep a method's old default; add the new behaviour under a new name. _(from a/b)_"]
    assert len([ln for ln in rules_markdown(cwd=str(tmp_path), records=RECS, limit=1).splitlines()
                if ln.startswith("- ")]) == 1


def test_rules_skip_what_agents_md_already_has(tmp_path):
    (tmp_path / "AGENTS.md").write_text("## Rules from whyagent\n\n- Never lower shared config defaults.\n")
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
