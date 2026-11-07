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
    """A -> B handoff: the proven guilty edit, plus what the rerun stage needs."""
    step: Step                # guilty edit (reasoning, trigger, prompt, diff)
    collateral: list[int]     # dependents reverted alongside it
    before_state: State       # P(k-1): rerun start state
    edits_before: list[Step]  # for the "summary of edits 1..k-1" prompt
    failing_output: str
    head_state: State         # for "everything that passed at G still passes"
    extra_causes: list[int] = field(default_factory=list)  # other edits from ddmin, if more than one cause
