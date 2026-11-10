"""Lesson generation, counterfactual reruns, and the statistical verdict."""
from __future__ import annotations

from math import comb


def fisher_one_sided(a_pass: int, a_n: int, b_pass: int, b_n: int) -> float:
    """P(B does at least this much better than A by chance). Hypergeometric upper tail."""
    total_pass, N = a_pass + b_pass, a_n + b_n
    denom = comb(N, total_pass)
    return sum(comb(b_n, x) * comb(a_n, total_pass - x)
               for x in range(b_pass, min(b_n, total_pass) + 1)
               if 0 <= total_pass - x <= a_n) / denom


def decide(a_pass: int, a_n: int, b_pass: int, b_n: int, alpha: float = 0.05) -> tuple[str, float]:
    """alpha: significance level; lower it when several lessons are tested against the same arm A."""
    p = fisher_one_sided(a_pass, a_n, b_pass, b_n)
    if a_pass >= 0.8 * a_n:
        return "NOT_NEEDED", p
    if p < alpha:
        return "VALIDATED", p
    if b_pass > a_pass:
        return "PROMISING", p
    return "INEFFECTIVE", p


from .harness import Agent, ClaudeCode, GeminiCLI, RerunResult, Reruns, Stub, agent_by_name, task_prompt  # noqa: E402,F401
from .lesson import make_lesson  # noqa: E402,F401
