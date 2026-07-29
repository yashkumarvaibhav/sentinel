-- A browser can request an effect, but only one worker may carry that request
-- into the world. The claim is deliberately separate from the public control
-- payload: it is an internal lease, not evidence about what happened.
--
-- CLAIMED means no actuator call has been dispatched. DISPATCHED is persisted
-- before crossing the external side-effect boundary, so a replacement worker
-- can distinguish "safe to retry" from "verify and fail closed; never guess".
CREATE TABLE IF NOT EXISTS {{schema}}.incident_action_execution_claims
(
    incident_id text NOT NULL,
    plan_revision bigint NOT NULL,
    claim_id text NOT NULL UNIQUE,
    worker_id text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('APPLY', 'ROLLBACK')),
    phase text NOT NULL CHECK (phase IN ('CLAIMED', 'DISPATCHED')),
    claimed_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > claimed_at),
    PRIMARY KEY (incident_id, plan_revision),
    FOREIGN KEY (incident_id, plan_revision)
        REFERENCES {{schema}}.incident_action_controls (incident_id, plan_revision)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS incident_action_execution_claims_expiry_idx
    ON {{schema}}.incident_action_execution_claims (expires_at);
