"""Live loop, write stage: add an approved rule to the agent's instructions file (AGENTS.md by default)."""
from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path

SECTION = "## Rules from whyagent"


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def has_rule(text: str, rule: str) -> bool:
    """True if `rule` is already a bullet in `text` (ignoring case, punctuation and spacing)."""
    want = _norm(rule)
    return any(_norm(ln.strip()[2:]) == want for ln in text.splitlines() if ln.strip().startswith(("- ", "* ")))


def provenance(test: str, session_id: str, step_id: int, file: str) -> str:
    m = re.search(r"\S+::\S+", test)
    test = m[0] if m else test
    return (f"<!-- whyagent: test={test} session={session_id[:8]} edit=#{step_id} ({file}) "
            f"{date.today().isoformat()} -->")


def append_rule(path: str | Path, rule: str, comment: str) -> bool:
    """Append `rule` (with its provenance comment) under the whyagent section. False if it's already there."""
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if has_rule(text, rule):
        return False
    add = ""
    if SECTION not in text:
        add += ("\n" if text and not text.endswith("\n") else "") + ("\n" if text else "") + SECTION + "\n"
    elif not text.endswith("\n"):
        add += "\n"
    add += f"\n{comment}\n- {rule}\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(add)
    return True


def worktree_files(source: str | Path, repo: str, base_commit: str, agent_name: str,
                   name: str = "AGENTS.md") -> dict[str, str]:
    """Files to drop into the rewound worktree so the rerun agent reads the rules.

    The rules always land in `name` (AGENTS.md) at the worktree root. If `source` is a different file (a
    throwaway rehearsal copy, say) and the rewound commit already has an AGENTS.md, the rules are appended
    to it rather than replacing it. Claude Code only reads CLAUDE.md, so it gets one that imports AGENTS.md.
    """
    src = Path(source).resolve()
    text = src.read_text(encoding="utf-8")
    if src != (Path(repo).resolve() / name):
        committed = _show(repo, base_commit, name)
        if committed:
            text = committed.rstrip("\n") + "\n\n" + text
    files = {name: text}
    if agent_name == "claude-code":
        claude = _show(repo, base_commit, "CLAUDE.md")
        files["CLAUDE.md"] = (claude.rstrip("\n") + "\n\n" if claude else "") + f"@{name}\n"
    return files


def _show(repo: str, commit: str, path: str) -> str:
    p = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True,
                       encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else ""
