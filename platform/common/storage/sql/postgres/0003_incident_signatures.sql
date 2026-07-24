-- Incident memory: the four-dimensional signature of every incident Sentinel
-- has finished measuring, so a new one can be asked "have we seen this shape
-- before?" The vector is the four independent axis scores at the incident's
-- peak, in a fixed order, which is exactly what the evidence agents already
-- produce -- no separate embedding model is involved.
--
-- Deliberately no ANN index. Exact search over a four-dimensional vector is
-- both correct and faster at this table's size; an ivfflat/hnsw index would
-- trade that exactness for nothing. Revisit only if this table grows past the
-- point where a sequential scan is measurably slow.
CREATE TABLE IF NOT EXISTS {{schema}}.incident_signatures
(
    incident_id text PRIMARY KEY,
    recorded_at timestamptz NOT NULL,
    origin_service text,
    verdict_class text,
    severity text NOT NULL,
    signature vector(4) NOT NULL,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS incident_signatures_recorded_idx
    ON {{schema}}.incident_signatures (recorded_at DESC);
