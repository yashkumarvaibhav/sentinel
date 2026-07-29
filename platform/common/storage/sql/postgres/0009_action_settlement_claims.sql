-- Target and rollback settlement are read-only work, but they still need a
-- lease so two gateway replicas cannot append competing verification facts.
-- They reuse the incident/revision claim row while remaining distinct from the
-- APPLY/ROLLBACK operations that cross the external dispatch boundary.
ALTER TABLE {{schema}}.incident_action_execution_claims
    DROP CONSTRAINT IF EXISTS incident_action_execution_claims_operation_check;

ALTER TABLE {{schema}}.incident_action_execution_claims
    ADD CONSTRAINT incident_action_execution_claims_operation_check
    CHECK (operation IN ('APPLY', 'ROLLBACK', 'VERIFY', 'VERIFY_ROLLBACK'));
