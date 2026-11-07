"""Shared types. All four modules meet here (see PRD: Data model)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

PASS = "PASS"
TARGET_FAIL = "TARGET_FAIL"
OTHER = "OTHER"


@dataclass
class Step:
    """One replayable agent edit."""
    id: int
    session_id: str
    agent: str
    file: str
    kind: str  # "edit" | "write_hunk" | "unattributed"
    old_text: str
    new_text: str
    reasoning: str | None = None
    trigger: str | None = None
    prompt: str = ""
    ts: str = ""  # ISO timestamp of the tool call; used to merge concurrent sessions
    command: str = ""  # for kind="shell": the shell command that made this change (e.g. sed -i)


@dataclass
class State:
    """A code snapshot we can test."""
    base_commit: str
    step_ids: list[int] = field(default_factory=list)
    pinned_tests_from: str = "HEAD"

    def key(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class RunResult:
    outcome: str  # PASS | TARGET_FAIL | OTHER
    signature: str
    exit_code: int
    duration_ms: int
    tokens: int | None = None
    output: str = ""  # tail of the test's stdout+stderr (for the failing output in reports and lessons)


@dataclass
class RootCause:
    """A -> B handoff: the proven guilty edit, and everything the reruns need."""
    step: Step                      # guilty edit (reasoning, trigger, prompt, diff)
    collateral: list[int]           # dependents reverted alongside it
    before_state: State             # P(k-1): rerun start state
    edits_before: list[Step]        # for the "summary of edits 1..k-1" prompt
    failing_output: str
    head_state: State               # for "everything that passed at G still passes"
    extra_causes: list[int] = field(default_factory=list)  # other edits from ddmin, if more than one cause

    @classmethod
    def from_dict(cls, d: dict) -> "RootCause":
        return cls(step=Step(**d["step"]), collateral=list(d.get("collateral", [])),
                   before_state=State(**d["before_state"]), edits_before=[Step(**s) for s in d["edits_before"]],
                   failing_output=d.get("failing_output", ""), head_state=State(**d["head_state"]),
                   extra_causes=list(d.get("extra_causes", [])))
