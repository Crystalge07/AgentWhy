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
