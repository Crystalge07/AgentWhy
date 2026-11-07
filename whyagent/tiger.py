"""Tiger Data run log: one row per test run or agent rerun (docs/tiger_schema.sql).

A no-op when TIGER_DATABASE_URL is unset or unreachable, so tests and offline runs still work.
"""
from __future__ import annotations

import os
import sys
import uuid


class RunLog:
    def __init__(self, analysis_id: str | None = None, pruned: bool = False, url: str | None = None):
        self.analysis_id = analysis_id or uuid.uuid4().hex[:12]
        self.pruned = pruned
        self._conn = None
        url = url if url is not None else os.environ.get("TIGER_DATABASE_URL")
        if url:
            try:
                import psycopg
                self._conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
            except Exception as e:  # never let logging break an analysis
                print(f"tiger: logging disabled ({type(e).__name__}: {e})", file=sys.stderr)

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def log(self, stage: str, state_hash: str, outcome: str, duration_ms: int,
            tokens: int | None = None, arm: str | None = None, cached: bool = False):
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO runs (analysis_id, pruned, stage, state_hash, outcome, duration_ms, tokens, arm, cached)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (self.analysis_id, self.pruned, stage, state_hash, outcome, duration_ms, tokens, arm, cached))
        except Exception as e:
            print(f"tiger: logging disabled after error ({type(e).__name__}: {e})", file=sys.stderr)
            self._conn = None

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
