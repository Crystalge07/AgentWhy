"""Tiger Data run log: one row per test run or agent rerun (docs/tiger_schema.sql).

A no-op when TIGER_DATABASE_URL is unset or unreachable, so tests and offline runs still work.
"""
from __future__ import annotations

import os
import sys
import threading
import uuid


ANALYSES_DDL = """CREATE TABLE IF NOT EXISTS analyses (
    analysis_id TEXT PRIMARY KEY,
    started     TIMESTAMPTZ NOT NULL DEFAULT now(),
    label       TEXT NOT NULL,      -- which bug: repo + test command
    pruned      BOOLEAN NOT NULL    -- Graph pruning used (set once Stage 4 verifies it)
)"""


def connect(url: str | None = None):
    import psycopg
    url = url if url is not None else os.environ.get("TIGER_DATABASE_URL")
    if not url:
        return None
    return psycopg.connect(url, autocommit=True, connect_timeout=10)


class RunLog:
    def __init__(self, analysis_id: str | None = None, pruned: bool = False, url: str | None = None,
                 label: str = ""):
        self.analysis_id = analysis_id or uuid.uuid4().hex[:12]
        self.pruned = pruned
        self.label = label
        self._conn = None
        self._lock = threading.Lock()  # the worker pool logs from several threads
        url = url if url is not None else os.environ.get("TIGER_DATABASE_URL")
        if url:
            try:
                self._conn = connect(url)
                self._conn.execute(ANALYSES_DDL)
                self._conn.execute("INSERT INTO analyses (analysis_id, label, pruned) VALUES (%s, %s, %s)"
                                   " ON CONFLICT (analysis_id) DO NOTHING", (self.analysis_id, label, pruned))
            except Exception as e:  # never let logging break an analysis
                print(f"tiger: logging disabled ({type(e).__name__}: {e})", file=sys.stderr)
                self._conn = None

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def log(self, stage: str, state_hash: str, outcome: str, duration_ms: int,
            tokens: int | None = None, arm: str | None = None, cached: bool = False):
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO runs (analysis_id, pruned, stage, state_hash, outcome, duration_ms, tokens, arm, cached)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (self.analysis_id, self.pruned, stage, state_hash, outcome, duration_ms, tokens, arm, cached))
        except Exception as e:
            print(f"tiger: logging disabled after error ({type(e).__name__}: {e})", file=sys.stderr)
            self._conn = None

    def set_pruned(self, pruned: bool):
        """Stage 4 calls this once Graph pruning is verified (or dropped)."""
        self.pruned = pruned
        if self._conn is not None:
            with self._lock:
                self._conn.execute("UPDATE analyses SET pruned = %s WHERE analysis_id = %s", (pruned, self.analysis_id))

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
