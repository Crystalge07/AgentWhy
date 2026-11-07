"""Thin wrapper around the `entire` CLI. Entire supplies the raw data; we do the analysis."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

TRAILER_RE = re.compile(r"^Entire-Checkpoint:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)


class EntireError(RuntimeError):
    pass


def _run(args: list[str], cwd: str = ".", check: bool = True) -> str:
    if not shutil.which("entire"):
        raise EntireError("`entire` CLI not found on PATH")
    p = subprocess.run(["entire", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise EntireError(f"entire {' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def _json(args: list[str], cwd: str = "."):
    out = _run([*args, "--json"], cwd=cwd)
    return json.loads(out) if out.strip() else None


def checkpoint_id(commit: str, cwd: str = ".") -> str | None:
    """Read the Entire checkpoint trailer on a commit. None means a human wrote it."""
    msg = subprocess.run(["git", "log", "-1", "--format=%B", commit], cwd=cwd,
                         capture_output=True, encoding="utf-8", errors="replace", check=True).stdout
    m = TRAILER_RE.search(msg)
    return m.group(1) if m else None


def status(cwd: str = ".") -> str:
    return _run(["status"], cwd=cwd)


def list_checkpoints(cwd: str = "."):
    return _json(["checkpoint", "list"], cwd=cwd)


def explain(ref: str, cwd: str = "."):
    return _json(["checkpoint", "explain", ref], cwd=cwd)


def session_info(session_id: str, cwd: str = "."):
    return _json(["session", "info", session_id], cwd=cwd)


def check_connection(cwd: str = ".") -> list[tuple[str, bool, str]]:
    """Smoke-test the Entire integration. Returns (check, ok, detail) rows."""
    rows: list[tuple[str, bool, str]] = []

    def attempt(name, fn):
        try:
            detail = fn()
            rows.append((name, True, detail))
        except Exception as e:  # noqa: BLE001
            rows.append((name, False, str(e).splitlines()[0] if str(e) else type(e).__name__))

    attempt("entire CLI on PATH", lambda: shutil.which("entire") or "")
    attempt("entire status", lambda: status(cwd).strip().splitlines()[0])
    attempt("git origin reachable", lambda: subprocess.run(
        ["git", "ls-remote", "--heads", "origin"], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
        check=True, timeout=30).stdout.strip().splitlines()[0])
    attempt("checkpoint list", lambda: f"{len(list_checkpoints(cwd) or [])} checkpoint(s)"
            if isinstance(list_checkpoints(cwd), list) else "ok")
    attempt("HEAD checkpoint trailer", lambda: checkpoint_id("HEAD", cwd) or "none (human commit)")
    return rows


# --- Brain: validated lessons ----------------------------------------------------------------
# Entire Brain has no CLI or API access for Entire-native repos (checked 2026-11-07), so lessons
# live in a JSON-lines file behind this interface (the PRD's fallback). B's rerun stage only talks
# to Brain through brain_lookup / brain_save, so the backend can change without touching it.

BRAIN_SOURCE = "local lesson store (Entire Brain has no CLI access)"
_EXC = re.compile(r"^(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning|Failure))\b:?\s*(.*)$")


def brain_path() -> "Path":
    import os
    from pathlib import Path

    return Path(os.environ.get("WHYAGENT_BRAIN") or Path.home() / ".whyagent" / "brain.jsonl")


def error_key(output: str) -> tuple[str, str]:
    """(exception type, normalized message) of the last exception line in a test's output."""
    for line in reversed(output.strip().splitlines()):
        m = _EXC.match(line.strip())
        if m:
            name = m[1].rsplit(".", 1)[-1]
            msg = re.sub(r"0x[0-9a-f]+|\d+|'[^']*'|\"[^\"]*\"", "#", m[2].lower()).strip()
            return name, msg[:200]
    return "", ""


def _brain_records() -> list[dict]:
    p = brain_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def brain_lookup(files: list[str], error: str) -> dict | None:
    """Best validated lesson for these files and this failure, or None.

    A lesson matches when it shares a file *and* the same exception type, or has an identical
    normalized error message. Ties go to the most recent lesson.
    """
    etype, emsg = error_key(error)
    best, best_score = None, 0
    for rec in _brain_records():
        overlap = bool(set(files) & set(rec.get("files", [])))
        same_type = bool(etype) and etype == rec.get("error_type")
        same_msg = bool(emsg) and emsg == rec.get("error_msg")
        if not (same_msg or (overlap and same_type)):
            continue
        score = 4 * same_msg + 2 * same_type + overlap
        if score >= best_score:
            best, best_score = rec, score
    return best


def brain_save(lesson: dict) -> dict:
    """Store a lesson. Only lessons that the counterfactual reruns VALIDATED are accepted.

    Required keys: text, files, failing_output, verdict ("VALIDATED"), p. Optional: checkpoint,
    session_id, agent, step_id, runs ({"a": [...], "b": [...]}).
    """
    from datetime import datetime, timezone

    if lesson.get("verdict") != "VALIDATED":
        raise ValueError(f"Brain only accepts validated lessons (verdict={lesson.get('verdict')!r})")
    for k in ("text", "files", "failing_output", "p"):
        if k not in lesson:
            raise ValueError(f"lesson is missing {k!r}")
    etype, emsg = error_key(lesson["failing_output"])
    rec = {k: v for k, v in lesson.items() if k != "failing_output"}
    rec.update(error_type=etype, error_msg=emsg,
               saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    p = brain_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec
