"""Parse Claude Code JSONL transcripts into ordered Steps."""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath, PureWindowsPath

from ..types import Step

EDIT_TOOLS = {"Edit", "MultiEdit", "Write"}
# Output the agent can be *reacting to*: commands it ran. File reads, searches and edit confirmations
# aren't triggers (any tool's error result still is).
TRIGGER_TOOLS = {"Bash", "PowerShell", "BashOutput"}
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
# The agent harness refusing a tool call is about permissions, not the code: never a trigger.
_HARNESS_DENIAL = re.compile(r"requires approval|requested permissions|permission to use|was blocked|"
                             r"user (doesn't|does not) want|tool use was rejected", re.I)
# Shell commands that only look at things. Their output is a file read, not something to react to.
_VIEWERS = {"cat", "head", "tail", "less", "more", "grep", "rg", "ls", "dir", "find", "tree", "wc", "od", "file",
            "stat", "pwd", "cd", "echo", "printf", "type", "which", "where", "Get-Content", "Get-ChildItem",
            "Select-String", "sort", "uniq", "cut", "awk", "nl", "true"}
_GIT_VIEWERS = {"ls-files", "show", "log", "diff", "status", "blame", "grep", "ls-tree", "rev-parse", "branch",
                "check-ignore", "cat-file"}


# An output redirect into a file (`> f`, `>> f`, `2> f`); not into /dev/null or another stream (`2>&1`).
_WRITE_REDIRECT = re.compile(r"(?<![<>&])(?:\d|&)?>>?(?!&)\s*(?!/dev/null\b)['\"]?[^\s&'\"]")


def _is_view_only(command: str) -> bool:
    """True if every part of a shell command (split on && || ; |) only views files or repo state.
    `echo x >> f` writes f, so any part that redirects output into a file isn't view-only."""
    for part in re.split(r"&&|\|\||;|\||\n", command or ""):
        if _WRITE_REDIRECT.search(part):
            return False
        words = [w for w in part.strip().split() if "=" not in w.split("/")[0]]  # skip VAR=x prefixes
        if not words:
            continue
        cmd = words[0].rsplit("/", 1)[-1]
        if cmd == "sed":
            if "-n" not in words and not any(w.startswith("-n") for w in words[1:]):
                return False
        elif cmd == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub not in _GIT_VIEWERS:
                return False
        elif cmd not in _VIEWERS and not cmd.startswith(("2>", ">", "<")):
            return False
    return True


def _rel(path: str, cwd: str | None) -> str:
    """Absolute path from the recording machine -> repo-relative POSIX path (works across OSes)."""
    if not cwd or not path:
        return path
    for P in (PurePosixPath, PureWindowsPath):
        try:
            return P(path).relative_to(P(cwd)).as_posix()
        except ValueError:
            pass
    return path


def _prompt_text(content) -> str:
    """User prompt with harness-injected <system-reminder> blocks removed."""
    return _REMINDER.sub("", _text_of(content)).strip()


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "tool_result":
                parts.append(_text_of(c.get("content")))
    return "\n".join(p for p in parts if p)


def _when(ts: str):
    """ISO timestamp -> aware datetime, or None if it can't be read."""
    from datetime import datetime, timezone

    try:
        d = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_claude_code(transcript: str, session_id: str = "", shell_out: list | None = None) -> list[Step]:
    """Edits from Edit/MultiEdit/Write calls. If `shell_out` is given, every shell command that isn't
    view-only is appended to it (with its context), so file changes made via `sed -i`, `git checkout --`
    and the like can be attributed later (adapters.fidelity.attribute_shell_edits)."""
    steps: list[Step] = []
    prompt = ""
    reasoning: str | None = None
    trigger: str | None = None
    seen: set[str] = set()  # Entire transcripts can repeat a tool_use record; apply each id once
    tool_names: dict[str, str] = {}  # tool_use id -> tool name
    cwd: str | None = None
    ts = ""

    def add(file, kind, old, new):
        file = _rel(file, cwd)
        steps.append(Step(id=len(steps) + 1, session_id=session_id, agent="claude-code", file=file,
                          kind=kind, old_text=old, new_text=new, reasoning=reasoning,
                          trigger=trigger, prompt=prompt, ts=ts))

    for line in transcript.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = session_id or rec.get("sessionId", "")
        cwd = rec.get("cwd") or cwd
        ts = rec.get("timestamp") or ts
        msg = rec.get("message") or {}
        content = msg.get("content")
        if rec.get("type") == "user":
            if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                for c in content:
                    if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                        continue
                    if tool_names.get(c.get("tool_use_id"), "") in TRIGGER_TOOLS or c.get("is_error"):
                        text = _text_of(c.get("content"))
                        if text and not _HARNESS_DENIAL.search(text):
                            trigger = text
            else:
                text = _prompt_text(content)
                if text and not text.startswith(("<local-command", "<command-")):  # /model etc. aren't prompts
                    prompt = text
                    reasoning, trigger = None, None
        elif rec.get("type") == "assistant" and isinstance(content, list):
            for c in content:
                t = c.get("type")
                if t in ("text", "thinking"):
                    txt = c.get("text") or c.get("thinking") or ""
                    if txt.strip():
                        reasoning = txt
                elif t == "tool_use" and c.get("id"):
                    name = c.get("name", "")
                    command = (c.get("input") or {}).get("command", "")
                    if name in TRIGGER_TOOLS and _is_view_only(command):
                        name += ":view"  # e.g. `cat file`: reading code, not reacting to an error
                    elif name in TRIGGER_TOOLS and shell_out is not None and c["id"] not in tool_names:
                        shell_out.append({"session_id": session_id, "ts": ts, "command": command, "cwd": cwd,
                                          "reasoning": reasoning, "trigger": trigger, "prompt": prompt})
                    tool_names.setdefault(c["id"], name)
                if t == "tool_use" and c.get("name") in EDIT_TOOLS:
                    if c.get("id"):
                        if c["id"] in seen:
                            continue
                        seen.add(c["id"])
                    inp = c.get("input", {})
                    f = inp.get("file_path", "")
                    # "write" (full file) and "edit_all" (replace_all) are turned into unique hunks
                    # later by adapters.fidelity.split_writes, once the real file content is known
                    if c["name"] == "Edit":
                        kind = "edit_all" if inp.get("replace_all") else "edit"
                        add(f, kind, inp.get("old_string", ""), inp.get("new_string", ""))
                    elif c["name"] == "MultiEdit":
                        for e in inp.get("edits", []):
                            kind = "edit_all" if e.get("replace_all") else "edit"
                            add(f, kind, e.get("old_string", ""), e.get("new_string", ""))
                    else:
                        add(f, "write", "", inp.get("content", ""))
    return steps
