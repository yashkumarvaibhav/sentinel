-- Operator control for authored lab runs. A paused run still owns the single
-- scenario slot: resuming it restarts the authored schedule, and another run
-- must never overlap that restart.
ALTER TABLE {{schema}}.lab_scenario_runs
    DROP CONSTRAINT IF EXISTS lab_scenario_runs_state_check;

ALTER TABLE {{schema}}.lab_scenario_runs
    ADD CONSTRAINT lab_scenario_runs_state_check CHECK (
        state IN ('QUEUED', 'RUNNING', 'PAUSED', 'SUCCEEDED', 'FAILED', 'STOPPED', 'REFUSED')
    );

ALTER TABLE {{schema}}.lab_scenario_runs
    ADD COLUMN IF NOT EXISTS control_requested text CHECK (
        control_requested IS NULL OR control_requested IN ('PAUSE', 'STOP')
    );

DROP INDEX IF EXISTS {{schema}}.lab_scenario_runs_one_in_flight_idx;

CREATE UNIQUE INDEX lab_scenario_runs_one_in_flight_idx
    ON {{schema}}.lab_scenario_runs ((state IN ('QUEUED', 'RUNNING', 'PAUSED')))
    WHERE state IN ('QUEUED', 'RUNNING', 'PAUSED');

CREATE TABLE IF NOT EXISTS {{schema}}.lab_runner_heartbeats
(
    worker_id text PRIMARY KEY,
    seen_at timestamptz NOT NULL,
    live_ready boolean NOT NULL,
    payload jsonb NOT NULL,
    CHECK ((payload ->> 'worker_id') = worker_id),
    CHECK ((payload ->> 'live_ready')::boolean = live_ready)
);

CREATE INDEX IF NOT EXISTS lab_runner_heartbeats_recent_idx
    ON {{schema}}.lab_runner_heartbeats (seen_at DESC);
