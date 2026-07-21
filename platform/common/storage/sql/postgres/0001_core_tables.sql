CREATE SCHEMA IF NOT EXISTS {{schema}};
CREATE SCHEMA IF NOT EXISTS {{dev_schema}};

CREATE TABLE IF NOT EXISTS {{schema}}.incidents
(
    incident_id text PRIMARY KEY,
    state text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT incidents_forward_time CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS incidents_updated_at_idx
    ON {{schema}}.incidents (updated_at DESC);

CREATE TABLE IF NOT EXISTS {{schema}}.audit_entries
(
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id text UNIQUE NOT NULL,
    ts timestamptz NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    prev_hash text NOT NULL,
    entry_hash text NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_entries_ts_idx
    ON {{schema}}.audit_entries (ts, sequence);

CREATE TABLE IF NOT EXISTS {{dev_schema}}.labels
(
    label_id text PRIMARY KEY,
    run_id text NOT NULL,
    subject_id text NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS labels_run_subject_idx
    ON {{dev_schema}}.labels (run_id, subject_id);
