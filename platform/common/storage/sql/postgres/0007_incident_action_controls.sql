-- An action control stores the exact evidence-owned plan revision separately
-- from the incident proof. Clients mutate only its state by intent; they never
-- replace the plan, rung, guards, revert token, or outcome in this payload.
CREATE TABLE IF NOT EXISTS {{schema}}.incident_action_controls
(
    incident_id text NOT NULL
        REFERENCES {{schema}}.incidents (incident_id),
    plan_revision bigint NOT NULL CHECK (plan_revision > 0),
    state text NOT NULL CHECK (
        state IN (
            'AWAITING_APPROVAL',
            'APPLY_REQUESTED',
            'REJECTED',
            'APPLIED',
            'VERIFIED',
            'FAILED',
            'ROLLBACK_REQUESTED',
            'ROLLED_BACK',
            'REFUSED',
            'SIMULATED'
        )
    ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    payload jsonb NOT NULL,
    PRIMARY KEY (incident_id, plan_revision),
    CHECK ((payload ->> 'incident_id') = incident_id),
    CHECK (((payload ->> 'plan_revision')::bigint) = plan_revision),
    CHECK ((payload ->> 'state') = state)
);

CREATE INDEX IF NOT EXISTS incident_action_controls_latest_idx
    ON {{schema}}.incident_action_controls (incident_id, plan_revision DESC);
