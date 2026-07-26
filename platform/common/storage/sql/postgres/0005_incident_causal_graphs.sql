-- A causal graph retains evidence the compact incident card deliberately omits.
-- It is runtime state, owned by the corresponding incident and advanced only
-- in the same transaction as that incident's durable revision.
CREATE TABLE IF NOT EXISTS {{schema}}.incident_causal_graphs
(
    incident_id text PRIMARY KEY
        REFERENCES {{schema}}.incidents (incident_id),
    updated_at timestamptz NOT NULL,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS incident_causal_graphs_latest_idx
    ON {{schema}}.incident_causal_graphs (updated_at DESC, incident_id ASC);
