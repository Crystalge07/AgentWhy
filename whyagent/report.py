"""Results report: a JSON-serializable record of one analysis, rendered to a self-contained HTML page.

The CLI fills a Report as stages run; `write()` saves report.json and report.html side by side.
The HTML embeds the JSON and renders it with inline JS, so it works offline and can be scp'd anywhere.
Schema (all keys optional except meta; the page shows whatever stages have data):

  meta        {repo, test, good, head, line, generated_at, sample}
  precheck    {head_fail, good_pass, n, mode, signature}
  commit      {sha, method: "blame"|"bisect", blamed, blame_confirmed, checkpoint, human}
  session     {id, agent, prompt, replay_matches, unattributed, steps: [Step-like dicts + line]}
  blame_hint  {line, step_ids, outcome, is_cause}
  pruning     {pruned: [ids], verified, runs_with, runs_without}
  bisect      {probes: [{after_step, outcome}], boundary}
  confirm     {step_id, collateral, outcome, ddmin}
  root_cause  {step_id, failing_output}
  lesson      {text, source: "gemini"|"brain"}
  rerun       {agent, a: [outcome], b: [outcome], p, verdict, saved_to_brain}
  runs        [{stage, label, outcome, duration_ms}]
  notes       [str]
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from .types import RunResult, State


class Report(dict):
    def __init__(self, **meta):
        super().__init__(meta={"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **meta},
                         runs=[], notes=[])

    def log_runs(self, stage: str, label: str, results: list[RunResult]):
        for r in results:
            self["runs"].append({"stage": stage, "label": label, "outcome": r.outcome,
                                 "duration_ms": r.duration_ms, "tokens": r.tokens})

    def note(self, text: str):
        self["notes"].append(text)

    def to_json(self) -> str:
        return json.dumps(self, indent=2, default=lambda o: asdict(o) if is_dataclass(o) else str(o))

    def write(self, html_path: str | Path) -> Path:
        html_path = Path(html_path)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.with_suffix(".json").write_text(self.to_json(), encoding="utf-8")
        html_path.write_text(render_html(json.loads(self.to_json())), encoding="utf-8")
        return html_path


def render_html(data: dict) -> str:
    template = resources.files("whyagent").joinpath("report.html").read_text(encoding="utf-8")
    # </ inside a <script> block would end it early
    payload = json.dumps(data).replace("</", "<\\/")
    return template.replace("/*__WHYAGENT_DATA__*/null", payload)


def state_label(state: State) -> str:
    c = state.base_commit[:7]
    return f"{c}+{len(state.step_ids)} edits" if state.step_ids else c
