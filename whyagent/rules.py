"""--rules: the validated rules from the Brain (Tiger Data), as Markdown for an agent's context.

Two levels of rules:
- per repo: the PR whyagent opens adds the rule to that repo's AGENTS.md, and every agent reads it there;
- per account: every validated rule lives in the Brain. A Claude Code SessionStart hook runs
  `whyagent --rules` and the agent starts each session with them, in any repo.

Rules already in this repo's AGENTS.md are skipped (the agent reads them there), and the list is capped
(newest first) so it doesn't crowd the agent's context. Tiger gets a short timeout and stays quiet:
the hook runs at every session start and must never block or spam it.
"""
from __future__ import annotations

from pathlib import Path

from . import entire
from .live.instructions import _norm, has_rule

HEADER = ("## Rules from agentwhy\n\n"
          "Each rule below was learned from a mistake an agent made in one of this user's repositories, and "
          "proven by reruns: agents given the rule fixed the task far more often than agents without it. "
          "Follow them unless the user says otherwise.\n")


def rules_markdown(instructions: str = "AGENTS.md", limit: int = 10, cwd: str = ".", records=None) -> str:
    """Markdown list of validated rules not already in `instructions`, newest first. "" if there are none."""
    recs = records if records is not None else entire._brain_records(timeout=3, quiet=True)
    p = Path(cwd) / instructions
    local = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    seen, out = set(), []
    for r in reversed(recs):  # newest first
        text = (r.get("text") or "").strip()
        key = _norm(text)
        if not text or key in seen or has_rule(local, text):
            continue
        seen.add(key)
        out.append(f"- {text}" + (f" _(from {r['repo']})_" if r.get("repo") else ""))
        if len(out) >= max(1, limit):
            break
    return (HEADER + "\n" + "\n".join(out)) if out else ""


# ---------------------------------------------------------------- the SessionStart hook

def claude_settings_path(config_dir: str | None = None) -> Path:
    """Claude Code's user settings: --claude-config, else $CLAUDE_CONFIG_DIR, else ~/.claude."""
    import os

    return Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def hook_command() -> str:
    """This install's interpreter running `--rules`. Works on Mac, Linux and Windows (forward slashes are fine
    in both Git Bash and cmd), and doesn't depend on whyagent being on PATH."""
    import sys

    return f'"{Path(sys.executable).as_posix()}" -m whyagent.cli --rules'


def _is_ours(h: dict) -> bool:
    c = h.get("command", "")
    return "whyagent.cli --rules" in c or "whyagent --rules" in c or "agentwhy --rules" in c


def _load(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8")) if path.exists() and path.read_text(encoding="utf-8").strip() else {}


def _save(path: Path, data: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.whyagent-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def install_hook(path: Path, command: str | None = None, timeout: int = 10) -> str:
    """Add (or update) the SessionStart hook that runs `--rules`. Returns "added", "updated" or "unchanged"."""
    command = command or hook_command()
    data = _load(path)
    groups = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    for g in groups:
        for h in g.get("hooks", []):
            if _is_ours(h):
                if h.get("command") == command and h.get("timeout") == timeout:
                    return "unchanged"
                h.update(type="command", command=command, timeout=timeout)
                _save(path, data)
                return "updated"
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
    _save(path, data)
    return "added"


def uninstall_hook(path: Path) -> bool:
    """Remove our SessionStart hook (other hooks stay). True if one was removed."""
    data = _load(path)
    groups = (data.get("hooks") or {}).get("SessionStart")
    if not groups:
        return False
    removed = False
    for g in groups:
        keep = [h for h in g.get("hooks", []) if not _is_ours(h)]
        removed |= len(keep) != len(g.get("hooks", []))
        g["hooks"] = keep
    data["hooks"]["SessionStart"] = [g for g in groups if g.get("hooks")]
    if not data["hooks"]["SessionStart"]:
        del data["hooks"]["SessionStart"]
    if not data["hooks"]:
        del data["hooks"]
    if removed:
        _save(path, data)
    return removed
