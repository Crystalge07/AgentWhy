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
# live in a `lessons` table in Tiger Data behind this interface, shared by the whole team. B's rerun stage only talks
# to Brain through brain_lookup / brain_save, so the backend can change without touching it.

BRAIN_SOURCE = "lesson store: Tiger Data, shared by the team (Entire Brain has no CLI access)"
LESSONS_DDL = "CREATE TABLE IF NOT EXISTS lessons (saved_at TIMESTAMPTZ NOT NULL DEFAULT now(), rec JSONB NOT NULL)"


def _tiger_lessons(timeout: int = 10, quiet: bool = False) -> list[dict]:
    """Brain: every validated lesson in Tiger Data, the one shared store, so every teammate's machine sees the
    same lessons (and a deleted one is gone for everyone). [] when Tiger is unset or unreachable."""
    import os
    import sys

    if not os.environ.get("TIGER_DATABASE_URL"):
        if not quiet:
            print("brain: TIGER_DATABASE_URL isn't set, so there are no lessons to look up", file=sys.stderr)
        return []
    try:
        from .tiger import connect

        with connect(timeout=timeout) as conn:
            conn.execute(LESSONS_DDL)
            return [r[0] for r in conn.execute("SELECT rec FROM lessons ORDER BY saved_at").fetchall()]
    except Exception as e:  # a lookup must never break an analysis: no lesson, write a new one
        if not quiet:
            print(f"brain: Tiger lessons unavailable ({type(e).__name__}: {e})", file=sys.stderr)
        return []


_EXC = re.compile(r"^(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning|Failure))\b:?\s*(.*)$")


def error_key(output: str) -> tuple[str, str]:
    """(exception type, normalized message) of the last exception line in a test's output."""
    for line in reversed(output.strip().splitlines()):
        m = _EXC.match(line.strip())
        if m:
            name = m[1].rsplit(".", 1)[-1]
            msg = re.sub(r"0x[0-9a-f]+|\d+|'[^']*'|\"[^\"]*\"", "#", m[2].lower()).strip()
            return name, msg[:200]
    return "", ""


def _brain_records(timeout: int = 10, quiet: bool = False) -> list[dict]:
    """Every validated lesson in Tiger Data, oldest first. Nothing is read from local files."""
    return sorted(_tiger_lessons(timeout=timeout, quiet=quiet), key=lambda r: r.get("saved_at") or "")


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


def repo_name(cwd: str = ".") -> str:
    """owner/repo from the origin (or first) remote's URL, else the folder name. Tags lessons by repo."""
    import subprocess
    from pathlib import Path

    for remote in ("origin", None):
        args = ["git", "remote", "get-url", remote] if remote else ["git", "remote"]
        p = subprocess.run(args, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
        out = p.stdout.strip()
        if p.returncode != 0 or not out:
            continue
        if remote is None:  # no origin: use the first remote
            p = subprocess.run(["git", "remote", "get-url", out.split()[0]], cwd=cwd, capture_output=True,
                               encoding="utf-8", errors="replace")
            out = p.stdout.strip()
        parts = [x for x in re.split(r"[/:]", re.sub(r"\.git$", "", out)) if x]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
    return Path(cwd).resolve().name


def brain_save(lesson: dict) -> dict:
    """Store a lesson in Tiger Data (never locally). Raises if Tiger is unset or the write fails. Only lessons that the counterfactual reruns VALIDATED are accepted.

    Required keys: text, files, failing_output, verdict ("VALIDATED"), p. Optional: repo (owner/name),
    checkpoint, session_id, agent, step_id, runs ({"a": [...], "b": [...]}).
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
    import os

    if not os.environ.get("TIGER_DATABASE_URL"):
        raise RuntimeError("Brain lives in Tiger Data only; set TIGER_DATABASE_URL to save lessons")
    from .tiger import connect

    with connect(timeout=10) as conn:  # errors propagate: a lesson that wasn't saved must say so
        conn.execute(LESSONS_DDL)
        conn.execute("INSERT INTO lessons (rec) VALUES (%s::jsonb)", (json.dumps(rec),))
    return rec
