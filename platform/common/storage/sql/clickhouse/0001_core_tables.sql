CREATE TABLE IF NOT EXISTS {{database}}.observations
(
    observation_id String,
    ts DateTime64(6, 'UTC'),
    service LowCardinality(String),
    signal LowCardinality(String),
    value Float64,
    unit LowCardinality(String),
    attributes_json String,
    flow_refs Array(String),
    log_refs Array(String),
    trace_refs Array(String),
    stored_at DateTime64(6, 'UTC') DEFAULT now64(6)
)
ENGINE = ReplacingMergeTree(stored_at)
PARTITION BY toYYYYMM(ts)
ORDER BY (service, signal, ts, observation_id)
TTL toDateTime(ts) + INTERVAL {{retention_days}} DAY DELETE;

CREATE TABLE IF NOT EXISTS {{database}}.decomp_frames
(
    frame_id String,
    observation_id String,
    ts DateTime64(6, 'UTC'),
    service LowCardinality(String),
    signal LowCardinality(String),
    observed Float64,
    explained_base Float64,
    explained_event Float64,
    residual Float64,
    band_low Float64,
    band_high Float64,
    residual_score Float64,
    context_ids Array(String),
    stored_at DateTime64(6, 'UTC') DEFAULT now64(6)
)
ENGINE = ReplacingMergeTree(stored_at)
PARTITION BY toYYYYMM(ts)
ORDER BY (service, signal, ts, frame_id)
TTL toDateTime(ts) + INTERVAL {{retention_days}} DAY DELETE;

CREATE TABLE IF NOT EXISTS {{database}}.symptoms
(
    symptom_id String,
    kind LowCardinality(String),
    service LowCardinality(String),
    signal LowCardinality(String),
    onset_ts DateTime64(6, 'UTC'),
    score Float64,
    note String,
    evidence_refs Array(String),
    stored_at DateTime64(6, 'UTC') DEFAULT now64(6),
    ts DateTime64(6, 'UTC') ALIAS onset_ts
)
ENGINE = ReplacingMergeTree(stored_at)
PARTITION BY toYYYYMM(onset_ts)
ORDER BY (service, signal, onset_ts, symptom_id)
TTL toDateTime(onset_ts) + INTERVAL {{retention_days}} DAY DELETE;
