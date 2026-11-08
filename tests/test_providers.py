"""Gemini first, Claude as fallback (whyagent/providers.py). No real API calls."""
import pytest

from whyagent import providers
from whyagent.rerun.lesson import write_lesson
from whyagent.types import Step


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    providers.reset()
    for k in ("GEMINI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    yield
    providers.reset()


def boom(prompt):
    raise RuntimeError("429 RESOURCE_EXHAUSTED. {'error': ...}")


def test_gemini_is_used_when_it_works(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    claude_calls = []
    text, who, tried = providers.generate("p", gemini=lambda p: "from gemini",
                                          claude=lambda p: claude_calls.append(p) or "from claude")
    assert (text, who, tried) == ("from gemini", "gemini", []) and claude_calls == []


def test_falls_back_to_claude_and_stops_retrying_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    gemini_calls = []

    def gemini(p):
        gemini_calls.append(p)
        boom(p)

    text, who, tried = providers.generate("p1", gemini=gemini, claude=lambda p: "from claude")
    assert (text, who) == ("from claude", "claude") and "RESOURCE_EXHAUSTED" in tried[0]
    providers.generate("p2", gemini=gemini, claude=lambda p: "again")
    assert gemini_calls == ["p1"]  # a failed Gemini isn't retried for the rest of the run


def test_no_gemini_key_goes_straight_to_claude():
    text, who, tried = providers.generate("p", gemini=boom, claude=lambda p: "c")
    assert who == "claude" and tried == ["Gemini skipped (GEMINI_API_KEY isn't set)"]


def test_both_failing_says_why():
    with pytest.raises(providers.ProviderError, match="GEMINI_API_KEY isn't set.*Claude failed"):
        providers.generate("p", gemini=boom, claude=boom)


def test_pick_rerun_agent(monkeypatch):
    assert providers.pick_rerun_agent("claude-code") == ("claude-code", None)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert providers.pick_rerun_agent("auto") == ("gemini-cli", None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    providers.mark_gemini_down("429 quota")
    name, why = providers.pick_rerun_agent("auto")
    assert name == "claude-code" and "429 quota" in why


def test_write_lesson_reports_the_provider():
    step = Step(1, "s", "claude-code", "a.py", "edit", "VERIFY = True", "VERIFY = False",
                reasoning="unblock", trigger="SSLError")
    lesson, who, tried = write_lesson(step, gemini=boom, claude=lambda p: 'Lesson: "Never disable TLS." Extra.')
    assert lesson == "Never disable TLS." and who == "claude" and tried
