CREATE TABLE IF NOT EXISTS {{schema}}.symptom_episodes
(
    episode_id text PRIMARY KEY,
    kind text NOT NULL,
    service text NOT NULL,
    signal text NOT NULL,
    status text NOT NULL,
    revision bigint NOT NULL,
    opened_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT symptom_episodes_forward_time CHECK (updated_at >= opened_at),
    CONSTRAINT symptom_episodes_positive_revision CHECK (revision >= 1)
);

CREATE INDEX IF NOT EXISTS symptom_episodes_status_updated_idx
    ON {{schema}}.symptom_episodes (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS symptom_episodes_key_idx
    ON {{schema}}.symptom_episodes (kind, service, signal);
