-- Security evidence advances with the incident card and full proof. The row
-- stores only public evidence projections; private scenario labels never enter
-- the runtime schema.
CREATE TABLE IF NOT EXISTS {{schema}}.incident_security_snapshots
(
    incident_id text PRIMARY KEY
        REFERENCES {{schema}}.incidents (incident_id),
    updated_at timestamptz NOT NULL,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS incident_security_snapshots_latest_idx
    ON {{schema}}.incident_security_snapshots (updated_at DESC, incident_id ASC);
