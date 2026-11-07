"""The mock rerun agent. Runs inside the rewound worktree (cwd) with $WHYAGENT_MOCK (JSON) as its config.

If the approved rule is in AGENTS.md, it makes the good fix. Without the rule it repeats the original
mistake, except on the runs listed in good_runs, so the rule stage has passing runs to compare against.
Standard library only: in Docker it runs as `python -c "$WHYAGENT_MOCK_AGENT"`.
"""
import json
import os
import sys
from pathlib import Path


def main() -> int:
    cfg = json.loads(os.environ["WHYAGENT_MOCK"])
    f = Path(cfg["instructions"])
    rule = cfg.get("rule") or ""
    has_rule = bool(rule) and f.exists() and rule in f.read_text(encoding="utf-8")
    run = int(os.environ.get("WHYAGENT_RUN_INDEX") or 0)
    good = has_rule or run in cfg["good_runs"]
    for rel, old, new in cfg["good" if good else "bad"]:
        p = Path(rel)
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        if old and text.count(old) != 1:
            print(f"mock agent: patch target not found exactly once in {rel}; the canned patch doesn't fit this state")
            return 3
        p.write_bytes((text.replace(old, new, 1) if old else text + new).encode("utf-8"))
    why = f"followed the rule in {f}" if has_rule else ("good fix (canned)" if good else "repeated the original edit")
    print(f"mock agent: run {run or '-'}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
