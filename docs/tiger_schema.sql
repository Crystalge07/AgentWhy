-- whyagent run log (Tiger Data / TimescaleDB)

-- One row per whyagent invocation; whyagent also creates this on first connect.
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id TEXT PRIMARY KEY,
    started     TIMESTAMPTZ NOT NULL DEFAULT now(),
    label       TEXT NOT NULL,      -- which bug: repo + test command
    pruned      BOOLEAN NOT NULL,   -- Graph pruning used (set once Stage 4 verifies it)
    blind_runs  INTEGER             -- blind bisect estimate: ceil(log2(non-test edits)) x runs per state
);
-- added after launch; whyagent runs this when it writes a report
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS blind_runs INTEGER;
-- where it ran (Vultr VM vs. a laptop) and when it finished: the Vultr speed comparison
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS host TEXT, ADD COLUMN IF NOT EXISTS cpus INTEGER,
    ADD COLUMN IF NOT EXISTS workers INTEGER, ADD COLUMN IF NOT EXISTS finished TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS runs (
    time         TIMESTAMPTZ NOT NULL DEFAULT now(),
    analysis_id  TEXT        NOT NULL,   -- one whyagent invocation
    pruned       BOOLEAN     NOT NULL,   -- Graph pruning on for this analysis?
    stage        TEXT        NOT NULL,   -- precheck, commit, blame, prune, bisect, confirm, ddmin, rerun
    state_hash   TEXT        NOT NULL,
    outcome      TEXT        NOT NULL,   -- PASS | TARGET_FAIL | OTHER
    duration_ms  INTEGER     NOT NULL,
    tokens       INTEGER,                -- agent reruns only
    arm          TEXT,                   -- rerun arm: 'A' (no lesson) | 'B' (lesson)
    cached       BOOLEAN     NOT NULL DEFAULT false
);

SELECT create_hypertable('runs', 'time', if_not_exists => TRUE);

-- Runs and tokens per stage, per analysis: the "Graph pruning saves runs" chart.
CREATE MATERIALIZED VIEW IF NOT EXISTS runs_by_stage
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', time) AS bucket,
       analysis_id, pruned, stage,
       count(*) FILTER (WHERE NOT cached) AS test_runs,
       coalesce(sum(tokens), 0)           AS tokens,
       sum(duration_ms)                   AS duration_ms
FROM runs
GROUP BY bucket, analysis_id, pruned, stage
WITH NO DATA;

SELECT add_continuous_aggregate_policy('runs_by_stage',
    start_offset => INTERVAL '1 day', end_offset => NULL,
    schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);
