"""Live progress events for `whyagent --serve`: one JSON object per line in <history>/<id>/events.jsonl.

An analysis turns them on with `start(dir)`; every other module just calls `emit(...)`, which is a no-op
when nothing is listening. The server tails the file, so a run started from the terminal shows up live too.

Event types: meta (once, at start), line (a line the CLI printed), state_start / state_done (a code state
being tested), rerun_done, result (a stage's result landed in the report), done (exit code). For the pipeline
view (/live): stage (a pipeline stage started, finished, was skipped or failed), commits (the commit search
range), steps (the session's edits as cards), lookup (Stage 7's lesson lookup), error.

`load_replay` reads a saved events.jsonl back with the pauses a replay should use.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_file = None


def start(directory: Path, **meta) -> None:
    """Write events for this process to directory/events.jsonl (and meta.json for the results list)."""
    global _file
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.json").write_text(json.dumps({**meta, "started": time.time()}), encoding="utf-8")
    _file = open(directory / "events.jsonl", "a", encoding="utf-8")
    emit("meta", **meta)
    sys.stdout = _Tee(sys.stdout)


def emit(type_: str, **data) -> None:
    if _file is None:
        return
    line = json.dumps({"t": round(time.time(), 2), "type": type_, **data}, default=str)
    with _lock:
        try:
            _file.write(line + "\n")
            _file.flush()
        except (OSError, ValueError):  # never let progress reporting break an analysis
            pass


def stage(name: str, status: str = "start", **data) -> None:
    """status: start | done | skip | error. The pipeline view lights the node up on start and colours it after."""
    emit("stage", name=name, status=status, **data)


# Replay pacing: real gaps between events, but never longer than MAX_GAP (a 40 s Docker build shouldn't stall the
# demo), and a beat after each result so a viewer can follow it. Both scale with the viewer's speed control.
MAX_GAP = 3.0
RESULT_PAUSE = 0.8
_RESULTS = {"state_done", "rerun_done", "result", "stage", "steps", "commits", "lookup"}


def load_replay(path: Path, max_gap: float = MAX_GAP, pause: float = RESULT_PAUSE) -> list[dict]:
    """Events of a saved run, each with `delay`: seconds to wait before showing it, at 1x speed. Bad lines are
    skipped (a run killed mid-write leaves half a line)."""
    out: list[dict] = []
    prev_t, prev_type = None, None
    for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(ev, dict) or "type" not in ev:
            continue
        t = ev.get("t") if isinstance(ev.get("t"), (int, float)) else prev_t
        gap = 0.0 if prev_t is None or t is None else min(max(t - prev_t, 0.0), max_gap)
        if prev_type in _RESULTS:
            gap = max(gap, pause)
        out.append({**ev, "delay": round(gap, 2)})
        prev_t, prev_type = t if t is not None else prev_t, ev["type"]
    return out


def active() -> bool:
    return _file is not None


class _Tee:
    """Passes stdout through and emits each printed line as a `line` event."""

    def __init__(self, inner):
        self._inner, self._buf = inner, ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            ln, self._buf = self._buf.split("\n", 1)
            if ln.strip():
                emit("line", text=ln)
        return self._inner.write(s)

    def flush(self):
        self._inner.flush()

    def __getattr__(self, name):
        return getattr(self._inner, name)
