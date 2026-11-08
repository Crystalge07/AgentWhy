"""Canned model responses for the seeded payments-api-client repo (WHYAGENT_MODEL=mock).

Each scenario: how to recognise its root cause, the rule "the model" writes, the good fix the mock agent
makes (applied to the rewound state, before the guilty edit), and which no-rule reruns make the good fix
anyway (so the rule stage has a pass/fail contrast). The bad fix is always the recorded guilty edit.
"""
from __future__ import annotations

from ..types import RootCause

SCENARIOS = [
    {
        "name": "2b-retries (payments/config.py: MAX_RETRIES/TIMEOUT_S changed globally)",
        "match": lambda rc: rc.step.file == "payments/config.py" and "MAX_RETRIES = 1" in rc.step.new_text,
        "rule": ("Don't change shared defaults in payments/config.py to tune one caller; add per-client "
                 "parameters (e.g. PaymentsClient constructor args) instead."),
        "good_runs": [2],
        "good": [
            ["payments/client.py",
             '''    def __init__(self, transport=None, base_url: str | None = None):
        self.transport = transport or HttpTransport()
        self.base_url = (base_url or config.API_BASE).rstrip("/")
        self.verify = config.VERIFY_TLS
''',
             '''    def __init__(self, transport=None, base_url: str | None = None, max_retries: int | None = None,
                 timeout_s: float | None = None):
        self.transport = transport or HttpTransport()
        self.base_url = (base_url or config.API_BASE).rstrip("/")
        self.verify = config.VERIFY_TLS
        self.max_retries = max_retries if max_retries is not None else config.MAX_RETRIES
        self.timeout_s = timeout_s if timeout_s is not None else config.TIMEOUT_S
'''],
            ["payments/client.py", "range(config.MAX_RETRIES)", "range(self.max_retries)"],
            ["payments/client.py", "timeout=config.TIMEOUT_S)", "timeout=self.timeout_s)"],
        ],
    },
]


def match(rc: RootCause) -> dict | None:
    return next((s for s in SCENARIOS if s["match"](rc)), None)
