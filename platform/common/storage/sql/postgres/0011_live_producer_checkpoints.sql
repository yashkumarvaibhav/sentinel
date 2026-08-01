-- Where the always-on live producer has durably judged its stream to. It moves
-- only after a tick's incident bundles are committed, so a restart re-consumes
-- the last tick instead of skipping it; the incident tables' own
-- stale-revision rule is what makes that re-consumption a no-op. It records a
-- position, never evidence: nothing here is read to make a judgement.
CREATE TABLE IF NOT EXISTS {{schema}}.live_producer_checkpoints
(
    producer_id text PRIMARY KEY,
    anchor_ts timestamptz NOT NULL,
    tick_ts timestamptz NOT NULL,
    published_incidents bigint NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT live_producer_checkpoints_ordered CHECK (tick_ts >= anchor_ts),
    CONSTRAINT live_producer_checkpoints_counted CHECK (published_incidents >= 0)
);
