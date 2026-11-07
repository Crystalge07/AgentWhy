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
    p = subprocess.run(["entire", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise EntireError(f"entire {' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def _json(args: list[str], cwd: str = "."):
    out = _run([*args, "--json"], cwd=cwd)
    return json.loads(out) if out.strip() else None


def checkpoint_id(commit: str, cwd: str = ".") -> str | None:
    """Read the Entire checkpoint trailer on a commit. None means a human wrote it."""
    msg = subprocess.run(["git", "log", "-1", "--format=%B", commit], cwd=cwd,
                         capture_output=True, text=True, check=True).stdout
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
        ["git", "ls-remote", "--heads", "origin"], cwd=cwd, capture_output=True, text=True,
        check=True, timeout=30).stdout.strip().splitlines()[0])
    attempt("checkpoint list", lambda: f"{len(list_checkpoints(cwd) or [])} checkpoint(s)"
            if isinstance(list_checkpoints(cwd), list) else "ok")
    attempt("HEAD checkpoint trailer", lambda: checkpoint_id("HEAD", cwd) or "none (human commit)")
    return rows
