"""Lesson generation, counterfactual reruns, and the statistical verdict."""
from __future__ import annotations

from math import comb

from ..types import Step


def fisher_one_sided(a_pass: int, a_n: int, b_pass: int, b_n: int) -> float:
    """P(B does at least this much better than A by chance). Hypergeometric upper tail."""
    total_pass, N = a_pass + b_pass, a_n + b_n
    denom = comb(N, total_pass)
    return sum(comb(b_n, x) * comb(a_n, total_pass - x)
               for x in range(b_pass, min(b_n, total_pass) + 1)
               if 0 <= total_pass - x <= a_n) / denom


def decide(a_pass: int, a_n: int, b_pass: int, b_n: int) -> tuple[str, float]:
    p = fisher_one_sided(a_pass, a_n, b_pass, b_n)
    if a_pass >= 0.8 * a_n:
        return "NOT_NEEDED", p
    if p < 0.05:
        return "VALIDATED", p
    if b_pass > a_pass:
        return "PROMISING", p
    return "INEFFECTIVE", p


def make_lesson(step: Step) -> str:
    """TODO(B, Stage 7): check Brain via entire.brain_lookup first, else call Gemini with trigger + reasoning + diff + failing output."""
    return f"Before changing {step.file}, fix the underlying cause instead of working around: {(step.trigger or '').splitlines()[-1:] or ''}"


def rerun(state, prompt: str, lesson: str | None, n: int):
    """TODO(B, Stage 8): run Gemini CLI headless in a sandbox, N times."""
    raise NotImplementedError
