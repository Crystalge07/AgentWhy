"""Stage 7: turn the guilty step into a one-sentence, imperative lesson (Gemini first, Claude via `claude -p`)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable

from ..types import RootCause, Step

DEFAULT_MODEL = "gemini-flash-latest"

PROMPT = """A coding agent made an edit that broke a test. We proved it by experiment: the test passes
right before this edit, fails right after it, and passes again when only this edit is reverted.

Task the agent was given:
{prompt}

What the agent was reacting to (tool output right before the edit):
{trigger}

The agent's reasoning right before the edit:
{reasoning}

The edit, in {file}:
--- before
{old}
+++ after
{new}

Failing test output at the end of the session:
{failing}

Write ONE lesson for a coding agent doing similar work, so it doesn't repeat this mistake.
Rules:
- One sentence, imperative mood, under 25 words.
- Name the bad move and the better alternative.
- General enough to apply beyond this exact line or file, but specific to this kind of mistake.
- No preamble, quotes or markdown. Output only the sentence."""


def _clip(s: str | None, n: int = 2500) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else "…" + s[-n:]


def build_prompt(step: Step, failing_output: str = "") -> str:
    return PROMPT.format(
        prompt=_clip(step.prompt, 800) or "(not recorded)",
        trigger=_clip(step.trigger) or "(none recorded)",
        reasoning=_clip(step.reasoning, 1500) or "(not recorded: the agent's reasoning was hidden; "
                                                 "infer the lesson from the trigger and the diff)",
        file=step.file, old=_clip(step.old_text, 1500) or "(new file)", new=_clip(step.new_text, 1500),
        failing=_clip(failing_output, 1500) or "(not available)")


def _clean(text: str) -> str:
    text = re.sub(r"^\s*(lesson\s*:)?\s*", "", text.strip(), flags=re.I).strip()
    first = re.split(r"(?<=[.!])[\"”'*`]*\s+", text)[0].strip().strip('"“”\'*` ')
    return first if first.endswith((".", "!")) else first + "."


def gemini_generate(prompt: str, model: str | None = None) -> str:
    from google import genai
    from google.genai import types

    # reads GEMINI_API_KEY. Pin v1beta: with newer (AQ.) keys the SDK defaults to v1, where the
    # "-latest" model aliases don't exist (404 NOT_FOUND).
    client = genai.Client(http_options=types.HttpOptions(api_version="v1beta"))
    model = model or os.environ.get("WHYAGENT_LESSON_MODEL", DEFAULT_MODEL)
    for attempt in range(4):  # 5xx (overloaded) is usually gone in seconds; 4xx (quota, auth) isn't
        try:
            return client.models.generate_content(model=model, contents=prompt).text or ""
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            if not (isinstance(code, int) and code >= 500) or attempt == 3:
                raise
            time.sleep(2 * 2 ** attempt)
    return ""


CLAUDE_AUTH = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def claude_missing() -> str | None:
    """Why claude_generate can't run, or None. A token is required on purpose: without one, `claude` would fall
    back to whichever account the host's default config is logged into."""
    if not shutil.which("claude"):
        return "the `claude` CLI isn't on PATH (npm install -g @anthropic-ai/claude-code)"
    if not any(os.environ.get(k) for k in CLAUDE_AUTH):
        return "set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) or ANTHROPIC_API_KEY"
    return None


def claude_generate(prompt: str, model: str | None = None, timeout: int = 180) -> str:
    """One headless Claude Code turn (`claude -p`), no tools. Auth: CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY.
    Model: WHYAGENT_CLAUDE_MODEL (e.g. haiku), else the account default."""
    missing = claude_missing()
    if missing:
        raise RuntimeError(missing)
    model = model or os.environ.get("WHYAGENT_CLAUDE_MODEL")
    cmd = [shutil.which("claude") or "claude", "-p", "--output-format", "json", "--max-turns", "1", "--tools", ""]
    if model:
        cmd += ["--model", model]
    env = {k: v for k, v in os.environ.items() if not (k in CLAUDE_AUTH and not v)}  # an empty key shadows the other
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    # An empty cwd: no CLAUDE.md, no project hooks (so Entire doesn't record this call as a session).
    with tempfile.TemporaryDirectory(prefix="whyagent-lesson-") as cwd:
        p = subprocess.run(cmd, input=prompt, cwd=cwd, env=env, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip()
    try:
        data = json.loads(out[out.find("{"):]) if "{" in out else {}
    except ValueError:
        data = {}
    if p.returncode != 0 or data.get("is_error") or not data.get("result"):
        why = data.get("result") or data.get("subtype") or (p.stderr or out).strip()[-300:] or f"exit {p.returncode}"
        raise RuntimeError(f"claude -p failed: {why}")
    return data["result"]


GENERATORS: dict[str, Callable[[str], str]] = {"gemini": gemini_generate, "claude": claude_generate}


def generator_missing(name: str) -> str | None:
    """Why the named lesson writer can't run (no key / no CLI), or None."""
    if name == "claude":
        return claude_missing()
    return None if os.environ.get("GEMINI_API_KEY") else "GEMINI_API_KEY isn't set"


def make_lesson(rc: RootCause | Step, generate: Callable[[str], str] = gemini_generate) -> str:
    """Write the lesson. `generate` is injectable so tests don't call the API."""
    step, failing = (rc.step, rc.failing_output) if isinstance(rc, RootCause) else (rc, "")
    return _clean(generate(build_prompt(step, failing)))


def write_lesson(rc: RootCause | Step, **providers_kw) -> tuple[str, str, list[str]]:
    """Stage 7 with provider fallback: (lesson, provider, why Gemini was skipped). Gemini first, then Claude."""
    from .. import providers

    used: dict = {}

    def gen(prompt: str) -> str:
        text, used["provider"], used["tried"] = providers.generate(prompt, **providers_kw)
        return text

    lesson = make_lesson(rc, generate=gen)
    return lesson, used["provider"], used["tried"]
