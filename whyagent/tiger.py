"""Tiger Data run log: one row per test run or agent rerun (docs/tiger_schema.sql).

A no-op when TIGER_DATABASE_URL is unset or unreachable, so tests and offline runs still work.
"""
from __future__ import annotations

import os
import platform
import sys
import threading
import uuid
from pathlib import Path


ANALYSES_DDL = """CREATE TABLE IF NOT EXISTS analyses (
    analysis_id TEXT PRIMARY KEY,
    started     TIMESTAMPTZ NOT NULL DEFAULT now(),
    label       TEXT NOT NULL,      -- which bug: repo + test command
    pruned      BOOLEAN NOT NULL    -- Graph pruning used (set once Stage 4 verifies it)
)"""


# One row per test run (same columns as docs/tiger_schema.sql), so logging works without applying the schema.
# The schema file also makes it a hypertable and adds the runs_by_stage view for the chart.
RUNS_DDL = """CREATE TABLE IF NOT EXISTS runs (
    time         TIMESTAMPTZ NOT NULL DEFAULT now(),
    analysis_id  TEXT        NOT NULL,
    pruned       BOOLEAN     NOT NULL,
    stage        TEXT        NOT NULL,
    state_hash   TEXT        NOT NULL,
    outcome      TEXT        NOT NULL,
    duration_ms  INTEGER     NOT NULL,
    tokens       INTEGER,
    arm          TEXT,
    cached       BOOLEAN     NOT NULL DEFAULT false
)"""


# Blind bisect estimate (runs a bisect over every non-test edit would need), set when the report is written.
BLIND_DDL = "ALTER TABLE analyses ADD COLUMN IF NOT EXISTS blind_runs INTEGER"


# Where an analysis ran, so the Vultr page can compare the same bug on a laptop vs. the VM.
HOST_DDL = ("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS host TEXT, ADD COLUMN IF NOT EXISTS cpus INTEGER,"
            " ADD COLUMN IF NOT EXISTS workers INTEGER, ADD COLUMN IF NOT EXISTS finished TIMESTAMPTZ")


def host_label(vendor_file: str = "/sys/class/dmi/id/sys_vendor") -> str:
    """'Vultr VM' on a Vultr instance (its DMI vendor says so), else the kind of machine. WHYAGENT_HOST overrides."""
    if os.environ.get("WHYAGENT_HOST"):
        return os.environ["WHYAGENT_HOST"]
    try:
        if Path(vendor_file).read_text(encoding="utf-8", errors="replace").strip().lower().startswith("vultr"):
            return "Vultr VM"
    except OSError:
        pass
    return {"Windows": "Laptop (Windows)", "Darwin": "Laptop (Mac)"}.get(platform.system(), f"{platform.system()} machine")


def connect(url: str | None = None, timeout: int = 10):
    import psycopg
    url = url if url is not None else os.environ.get("TIGER_DATABASE_URL")
    if not url:
        return None
    return psycopg.connect(url, autocommit=True, connect_timeout=timeout)


class RunLog:
    def __init__(self, analysis_id: str | None = None, pruned: bool = False, url: str | None = None,
                 label: str = "", workers: int | None = None):
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
                self._conn.execute(HOST_DDL)
                self._conn.execute(RUNS_DDL)
                self._conn.execute("INSERT INTO analyses (analysis_id, label, pruned, host, cpus, workers)"
                                   " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (analysis_id) DO NOTHING",
                                   (self.analysis_id, label, pruned, host_label(), os.cpu_count(), workers))
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
            try:  # wall-clock end of the analysis, for the speed comparison
                with self._lock:
                    self._conn.execute("UPDATE analyses SET finished = now() WHERE analysis_id = %s AND finished IS NULL",
                                       (self.analysis_id,))
            except Exception:  # noqa: BLE001
                pass
            self._conn.close()
            self._conn = None
