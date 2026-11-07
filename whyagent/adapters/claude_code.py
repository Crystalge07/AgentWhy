"""Parse Claude Code JSONL transcripts into ordered Steps."""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath, PureWindowsPath

from ..types import Step

EDIT_TOOLS = {"Edit", "MultiEdit", "Write"}
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


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


def parse_claude_code(transcript: str, session_id: str = "") -> list[Step]:
    steps: list[Step] = []
    prompt = ""
    reasoning: str | None = None
    trigger: str | None = None
    seen: set[str] = set()  # Entire transcripts can repeat a tool_use record; apply each id once
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
                trigger = _text_of(content) or trigger
            else:
                prompt = _prompt_text(content) or prompt
                reasoning, trigger = None, None
        elif rec.get("type") == "assistant" and isinstance(content, list):
            for c in content:
                t = c.get("type")
                if t in ("text", "thinking"):
                    txt = c.get("text") or c.get("thinking") or ""
                    if txt.strip():
                        reasoning = txt
                elif t == "tool_use" and c.get("name") in EDIT_TOOLS:
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
