"""Stage 7: turn the guilty step into a one-sentence, imperative lesson (Gemini, Claude as fallback)."""
from __future__ import annotations

import os
import re
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
