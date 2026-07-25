-- The tamper-evident ledger: every decision, action and rollback, hash-chained.
--
-- This SUPERSEDES the `audit_entries` table in 0001, which was scaffolding put
-- in before the `AuditEntry` contract existed. That table lets its caller supply
-- both `prev_hash` and `entry_hash`, so two writers that read the same head can
-- both insert and the chain forks with the database's blessing. It is left in
-- place rather than dropped because dropping a table is irreversible and it
-- holds nothing; nothing writes to it outside its own tests. Retiring it is a
-- deliberate step, not a side effect of this migration.
--
-- Two constraints do the real work here, and they are belt and braces on
-- purpose:
--
--   * UNIQUE (sequence) -- an entry may occupy one place in the chain.
--   * UNIQUE (previous_hash) -- and, more importantly, no two entries may build
--     on the SAME entry. That single constraint makes a fork impossible at the
--     storage layer even if the advisory lock the repository takes were removed
--     or misused: the second writer's insert fails rather than silently
--     producing two valid-looking histories. A lock prevents the race; this
--     detects the outcome the race would have had.
--
-- `sequence` is supplied by the writer rather than generated, because it is
-- inside the entry's own hash. A database-generated identity could not be known
-- before the row was written, and a sequence outside the hash would let an entry
-- be moved without breaking anything.
CREATE TABLE IF NOT EXISTS {{schema}}.audit_ledger
(
    entry_id text PRIMARY KEY,
    sequence bigint NOT NULL UNIQUE,
    ts timestamptz NOT NULL,
    kind text NOT NULL,
    actor text NOT NULL,
    summary text NOT NULL,
    incident_id text,
    decision_id text,
    plan_id text,
    body jsonb NOT NULL,
    previous_hash text NOT NULL UNIQUE,
    entry_hash text NOT NULL UNIQUE,
    honesty text NOT NULL
);

-- Reading the ledger is almost always "what happened, in order", so the index
-- that matters is the sequence one the UNIQUE constraint already provides.
-- These two serve the questions a person actually asks of an audit trail.
CREATE INDEX IF NOT EXISTS audit_ledger_ts_idx
    ON {{schema}}.audit_ledger (ts DESC, sequence DESC);

CREATE INDEX IF NOT EXISTS audit_ledger_incident_idx
    ON {{schema}}.audit_ledger (incident_id, sequence)
    WHERE incident_id IS NOT NULL;
