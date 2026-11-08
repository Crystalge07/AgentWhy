"""Data for the results site's Tiger Data and Vultr pages (/tiger, /vultr), queried live from TIGER_DATABASE_URL.

Every number on those pages comes from these queries, and each query's SQL and run time are returned with its
result so the page can show exactly what it asked Tiger. Rows from the unit-test suite (labelled "repo: ...",
written before tests stopped logging to the shared database) are left out.
"""
from __future__ import annotations

import time

from .chart import bug_name

REAL = "a.label NOT LIKE 'repo:%'"  # unit-test fixtures, not real analyses

Q = {
    "totals": f"""
SELECT count(DISTINCT a.analysis_id)                                   AS analyses,
       count(*) FILTER (WHERE r.stage <> 'rerun' AND NOT r.cached)     AS test_runs,
       count(*) FILTER (WHERE r.stage <> 'rerun' AND r.cached)         AS reused,
       count(*) FILTER (WHERE r.stage = 'rerun')                       AS reruns,
       round(sum(r.duration_ms) FILTER (WHERE NOT r.cached) / 3600000.0, 1) AS hours
FROM runs r JOIN analyses a USING (analysis_id)
WHERE {REAL}""",
    "timeline": f"""
SELECT time_bucket('30 minutes', r.time) AS bucket,
       CASE WHEN r.outcome = 'PASS' THEN 'PASS'
            WHEN r.outcome IN ('TARGET_FAIL', 'FAIL', 'REGRESSED') THEN 'FAIL' ELSE 'OTHER' END AS outcome,
       count(*) AS n
FROM runs r JOIN analyses a USING (analysis_id)
WHERE {REAL} AND NOT r.cached AND r.stage <> 'rerun'
GROUP BY 1, 2 ORDER BY 1""",
    "stages": f"""
SELECT r.stage, count(*) AS runs, round(avg(r.duration_ms))::int AS avg_ms
FROM runs r JOIN analyses a USING (analysis_id)
WHERE {REAL} AND NOT r.cached
GROUP BY 1 ORDER BY 2 DESC""",
    "aggregate": """
SELECT sum(test_runs)::int AS test_runs, count(DISTINCT analysis_id) AS analyses
FROM runs_by_stage""",
    "hypertable": """
SELECT h.num_chunks, (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') AS version
FROM timescaledb_information.hypertables h WHERE h.hypertable_name = 'runs'""",
    "lessons": """
SELECT saved_at, rec->>'text' AS text, rec->>'verdict' AS verdict, (rec->>'p')::float AS p, rec->>'repo' AS repo,
       rec->'runs' AS runs
FROM lessons ORDER BY saved_at""",
    "speed": """
SELECT a.analysis_id, a.label, a.host, a.workers, a.cpus,
       -- time to the proven root cause: start to the last test run (lesson writing and reruns depend on model APIs)
       extract(epoch FROM max(r.time) FILTER (WHERE r.stage <> 'rerun') - a.started)::float AS wall_s,
       count(*) FILTER (WHERE NOT r.cached AND r.stage <> 'rerun') AS test_runs,
       avg(r.duration_ms) FILTER (WHERE NOT r.cached AND r.stage <> 'rerun')::float AS ms_per_run,
       a.started
FROM analyses a JOIN runs r USING (analysis_id)
WHERE a.host IS NOT NULL AND a.label NOT LIKE 'repo:%'
GROUP BY a.analysis_id ORDER BY a.started""",
}


def _query(conn, name: str) -> dict:
    t0 = time.perf_counter()
    cur = conn.execute(Q[name])
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"sql": Q[name].strip(), "ms": round((time.perf_counter() - t0) * 1000), "rows": rows}


def _plain(v):
    if hasattr(v, "isoformat"):
        return v.isoformat(timespec="minutes")
    if v.__class__.__name__ == "Decimal":
        return float(v)
    return v


def _clean(q: dict) -> dict:
    q["rows"] = [{k: _plain(v) for k, v in r.items()} for r in q["rows"]]
    return q


def tiger(conn) -> dict:
    try:
        conn.execute("CALL refresh_continuous_aggregate('runs_by_stage', NULL, NULL)")
    except Exception:  # noqa: BLE001 — no permission: the aggregate is still refreshed by its policy every minute
        pass
    out = {k: _clean(_query(conn, k)) for k in ("totals", "timeline", "stages", "aggregate", "hypertable", "lessons")}
    for r in out["lessons"]["rows"]:  # without / with lesson pass counts, from the saved rerun outcomes
        runs = r.pop("runs") or {}
        a, b = runs.get("a") or [], runs.get("b") or []
        r.update(a_pass=sum(x == "PASS" for x in a), a_n=len(a), b_pass=sum(x == "PASS" for x in b), b_n=len(b))
    return out


def speed_compare(rows: list[dict]) -> list[dict]:
    """The latest analysis of each bug on each host; bugs measured on a Vultr VM and somewhere else are compared."""
    latest: dict[tuple, dict] = {}
    for r in rows:
        if r["test_runs"]:
            latest[(bug_name(r["label"]), r["host"])] = r
    bugs: dict[str, dict] = {}
    for (bug, host), r in latest.items():
        bugs.setdefault(bug, {})[host] = r
    out = []
    for bug, hosts in bugs.items():
        vm = next((r for h, r in hosts.items() if h.startswith("Vultr")), None)
        others = [r for h, r in hosts.items() if not h.startswith("Vultr")]
        if vm and others:
            lap = others[-1]
            out.append({"bug": bug.split("::")[-1], "vm": vm, "laptop": lap,
                        "speedup": round(lap["wall_s"] / vm["wall_s"], 1) if vm["wall_s"] else None,
                        "per_run_speedup": round(lap["ms_per_run"] / vm["ms_per_run"], 1) if vm["ms_per_run"] else None})
    return out


def vultr(conn) -> dict:
    q = _clean(_query(conn, "speed"))
    hosts: dict[str, dict] = {}
    for r in q["rows"]:
        h = hosts.setdefault(r["host"], {"host": r["host"], "analyses": 0, "test_runs": 0, "run_ms": 0.0, "cpus": r["cpus"]})
        h["analyses"] += 1
        h["test_runs"] += r["test_runs"]
        h["run_ms"] += (r["ms_per_run"] or 0) * r["test_runs"]
    for h in hosts.values():
        h["ms_per_run"] = round(h.pop("run_ms") / h["test_runs"]) if h["test_runs"] else None
    return {"speed": q, "compare": speed_compare(q["rows"]), "hosts": list(hosts.values())}
