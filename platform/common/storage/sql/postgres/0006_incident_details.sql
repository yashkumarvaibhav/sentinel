-- The proof screen retains evidence the compact incident card deliberately
-- omits. It advances atomically with that card and its causal graph so a reader
-- can never combine different decision revisions.
CREATE TABLE IF NOT EXISTS {{schema}}.incident_details
(
    incident_id text PRIMARY KEY
        REFERENCES {{schema}}.incidents (incident_id),
    updated_at timestamptz NOT NULL,
    payload jsonb NOT NULL
);
