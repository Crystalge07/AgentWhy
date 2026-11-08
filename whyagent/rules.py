"""--rules: the validated rules from the Brain (local file + Tiger Data), as Markdown for an agent's context.

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

HEADER = ("## Rules from whyagent\n\n"
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
