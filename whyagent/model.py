"""One interface for every model call the live loop makes: writing the rule and rerunning the agent.

    WHYAGENT_MODEL=mock   (or --model mock)    canned responses for the seeded demo repo, no network
    WHYAGENT_MODEL=gemini (default)            Gemini writes the rule; the rerun agent is --rerun-agent
    WHYAGENT_MODEL=claude                      `claude -p` writes the rule (CLAUDE_CODE_OAUTH_TOKEN or
                                               ANTHROPIC_API_KEY, model WHYAGENT_CLAUDE_MODEL); reruns default
                                               to claude-code

The mock is for demos and rehearsals while we have no Gemini access. Its test runs are real (the
target test runs on whatever the mock agent wrote); only the "model" output is canned.
"""
from __future__ import annotations

import json
import os
import sys
from importlib import resources

from .rerun.harness import Agent, agent_by_name
from .types import RootCause

AGENT_INSTRUCTIONS = "AGENTS.md"  # the file name rerun agents read inside the rewound worktree
MODELS = ("gemini", "claude", "mock")
DEFAULT_AGENT = {"gemini": "auto", "claude": "claude-code"}  # auto: Gemini CLI, Claude Code if Gemini fails


class ModelError(RuntimeError):
    """The model can't be used (no key, no canned response). The message says why."""


class Model:
    name = "model"

    def missing_auth(self) -> str | None:
        return None

    def generate(self, prompt: str) -> str:
        raise NotImplementedError

    def agent(self, rule: str | None = None) -> Agent:
        """The rerun agent. `rule` is the approved rule (the real agent only sees it in AGENTS.md)."""
        raise NotImplementedError


class GeminiModel(Model):
    """Gemini first, Claude if Gemini is unavailable or fails (see whyagent/providers.py)."""
    name = "gemini"

    def __init__(self, rerun_agent: str = "auto"):
        self.rerun_agent = rerun_agent
        self.provider: str | None = None  # who actually wrote the rule: "gemini" or "claude"

    def missing_auth(self) -> str | None:
        from . import providers

        if not (providers.gemini_configured() or providers.claude_configured()):
            return ("neither GEMINI_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY is set "
                    "(use --model mock or WHYAGENT_MODEL=mock to run on canned responses)")
        return agent_by_name(self._agent_name()).missing_auth()

    def generate(self, prompt: str) -> str:
        from . import providers

        text, self.provider, tried = providers.generate(prompt)
        for t in tried:
            print(f"model: {t} → Claude", file=sys.stderr)
        return text

    def _agent_name(self) -> str:
        from . import providers

        return providers.pick_rerun_agent(self.rerun_agent)[0]

    def agent(self, rule: str | None = None) -> Agent:
        return agent_by_name(self._agent_name(), context_file=AGENT_INSTRUCTIONS)


class ClaudeModel(GeminiModel):
    """Claude Code headless writes the rule (`claude -p`, no tools); reruns use --rerun-agent (default claude-code)."""
    name = "claude"

    def __init__(self, rerun_agent: str = "claude-code"):
        super().__init__(rerun_agent)

    def missing_auth(self) -> str | None:
        from .rerun.lesson import claude_missing
        return claude_missing() or agent_by_name(self.rerun_agent).missing_auth()

    def generate(self, prompt: str) -> str:
        from .rerun.lesson import claude_generate
        return claude_generate(prompt)


class MockAgent(Agent):
    def error(self, output: str, exit_code: int) -> str | None:
        if "mock agent:" in output and exit_code != 0:
            return output.strip().splitlines()[-1]
        return super().error(output, exit_code)


class MockModel(Model):
    """Canned responses for the seeded payments-api-client scenarios (see live/mock_data.py)."""
    name = "mock"

    def __init__(self, local: bool = False):
        self.local = local  # LocalSandbox: run the mock agent with this interpreter instead of the image's `python`
        self.scenario: dict | None = None
        self.rc: RootCause | None = None

    def bind(self, rc: RootCause) -> dict:
        """Pick the canned scenario for this root cause, or fail with what the mock covers."""
        from .live.mock_data import match
        self.rc, self.scenario = rc, match(rc)
        if self.scenario is None:
            from .live.mock_data import SCENARIOS
            raise ModelError(f"no canned response for edit #{rc.step.id} in {rc.step.file}; the mock covers: "
                             + ", ".join(s["name"] for s in SCENARIOS))
        return self.scenario

    def generate(self, prompt: str) -> str:
        if self.scenario is None:
            raise ModelError("mock model isn't bound to a root cause")
        return self.scenario["rule"]

    def agent(self, rule: str | None = None) -> Agent:
        if self.scenario is None or self.rc is None:
            raise ModelError("mock model isn't bound to a root cause")
        cfg = {
            "instructions": AGENT_INSTRUCTIONS,
            "rule": rule or "",
            "good_runs": self.scenario["good_runs"],
            "good": self.scenario["good"],
            "bad": [[self.rc.step.file, self.rc.step.old_text, self.rc.step.new_text]],  # the original mistake
        }
        if self.local:
            command = f'"{sys.executable}" -m whyagent.live.mock_agent'
        else:  # the sandbox image doesn't have whyagent installed: pass the script's source instead
            command = 'python -c "$WHYAGENT_MOCK_AGENT"'
        src = resources.files("whyagent.live").joinpath("mock_agent.py").read_text(encoding="utf-8")
        return MockAgent(name="mock", command=command, network="none",
                         env={"WHYAGENT_MOCK": json.dumps(cfg), "WHYAGENT_MOCK_AGENT": src})


def model_name(name: str | None) -> str:
    """--model, else $WHYAGENT_MODEL, else gemini."""
    return (name or os.environ.get("WHYAGENT_MODEL") or "gemini").strip().lower()


def make_model(name: str | None, rerun_agent: str | None = None, local: bool = False) -> Model:
    name = model_name(name)
    if name == "mock":
        return MockModel(local=local)
    if name == "gemini":
        return GeminiModel(rerun_agent or DEFAULT_AGENT[name])
    if name == "claude":
        return ClaudeModel(rerun_agent or DEFAULT_AGENT[name])
    raise ModelError(f"unknown model {name!r} (WHYAGENT_MODEL / --model: {', '.join(MODELS)})")
