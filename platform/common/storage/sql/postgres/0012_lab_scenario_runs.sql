-- Requested scenario runs. The public API writes rows here and never executes
-- one: the gateway image carries no `lab/`, which is what keeps scenario ground
-- truth unreachable from the process that serves the public API. A lab-side
-- runner with the repo mounted claims a row and does the work.
--
-- ONE RUN AT A TIME is enforced by the partial unique index below rather than
-- by the runner remembering to check. Two scenarios overlapping would put two
-- sets of injected faults into one stretch of telemetry, and every label either
-- of them carries would then describe traffic the other one also caused.
CREATE TABLE IF NOT EXISTS {{schema}}.lab_scenario_runs
(
    run_id text PRIMARY KEY,
    scenario_id text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('REPLAY', 'LIVE')),
    state text NOT NULL CHECK (
        state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'REFUSED')
    ),
    requested_at timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    -- Which worker holds it, so an abandoned claim is visible rather than
    -- indistinguishable from a slow one.
    claimed_by text,
    claim_expires_at timestamptz,
    payload jsonb NOT NULL,
    CHECK ((payload ->> 'run_id') = run_id),
    CHECK ((payload ->> 'state') = state),
    CHECK (started_at IS NULL OR started_at >= requested_at),
    CHECK (finished_at IS NULL OR finished_at >= COALESCE(started_at, requested_at))
);

-- At most one run that has not finished, across the whole deployment.
CREATE UNIQUE INDEX IF NOT EXISTS lab_scenario_runs_one_in_flight_idx
    ON {{schema}}.lab_scenario_runs ((state IN ('QUEUED', 'RUNNING')))
    WHERE state IN ('QUEUED', 'RUNNING');

CREATE INDEX IF NOT EXISTS lab_scenario_runs_recent_idx
    ON {{schema}}.lab_scenario_runs (requested_at DESC);
