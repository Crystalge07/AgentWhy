"""Which model provider does the work: Gemini first, Claude if Gemini can't.

Every model call in whyagent (Stage 7 lesson, Stage 8 rerun agent, the --live loop's rule) goes
through here. Gemini is tried first; if it isn't configured, is out of quota or errors, the call
falls back to Claude (Claude Code CLI with CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, or
ANTHROPIC_API_KEY). Once Gemini fails in a process it isn't retried for the rest of that run, so a
quota-exhausted key costs one call, not one per stage. The provider that actually answered is
returned so reports and Tiger say which one did the work.
"""
from __future__ import annotations

import os

GEMINI, CLAUDE = "gemini", "claude"
_gemini_down: str | None = None  # why Gemini failed earlier in this process (skip it from then on)


class ProviderError(RuntimeError):
    pass


def gemini_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def claude_configured() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))


def gemini_status() -> str | None:
    """None if Gemini is worth trying, else why not."""
    if not gemini_configured():
        return "GEMINI_API_KEY isn't set"
    return _gemini_down


def mark_gemini_down(reason: str) -> None:
    global _gemini_down
    _gemini_down = reason


def reset() -> None:  # tests
    global _gemini_down
    _gemini_down = None


def _short(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e).split('. {')[0][:160]}"


def generate(prompt: str, gemini=None, claude=None) -> tuple[str, str, list[str]]:
    """(text, provider, fallbacks): Gemini first, then Claude. `fallbacks` says why Gemini was skipped.

    `gemini` / `claude` are injectable for tests (default: the real API calls).
    """
    if gemini is None:
        from .rerun.lesson import gemini_generate as gemini
    if claude is None:
        from .rerun.lesson import claude_generate as claude  # one Claude caller for everyone
    tried: list[str] = []
    why = gemini_status()
    if why is None:
        try:
            return gemini(prompt), GEMINI, tried
        except Exception as e:  # noqa: BLE001 — quota, auth, network: fall back, don't crash
            why = _short(e)
            mark_gemini_down(why)
    tried.append(f"Gemini skipped ({why})")
    try:
        return claude(prompt), CLAUDE, tried
    except Exception as e:  # noqa: BLE001
        raise ProviderError("; ".join(tried + [f"Claude failed ({_short(e)})"])) from e


def pick_rerun_agent(choice: str) -> tuple[str, str | None]:
    """--rerun-agent auto: Gemini CLI if Gemini works, else Claude Code. Returns (agent name, fallback reason)."""
    if choice != "auto":
        return choice, None
    why = gemini_status()
    if why is None:
        return "gemini-cli", None
    return ("claude-code", f"Gemini unavailable ({why})") if claude_configured() else ("gemini-cli", None)
