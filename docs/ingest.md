# Ingest normalizer

The Collector writes the same incoming OTLP telemetry to the operational stores
and to three raw Redpanda topics. The ingest service is the only component that
turns those raw envelopes into Sentinel `Observation` records.

| Input | OTLP encoding | Canonical output |
|---|---|---|
| `otlp.raw.metrics` | `otlp_json` | gauge/sum points; histogram, exponential-histogram and summary count/sum points |
| `otlp.raw.logs` | `otlp_json` | `log.record`, with a deterministic log evidence reference |
| `otlp.raw.traces` | `otlp_json` | `span.duration_ms`, with its trace reference |

Every output is published to `obs.normalized` with `(service, signal)` as its
Kafka key and is also inserted into ClickHouse. JSON object order, Collector
batching and redelivery do not change the observation ID: it is SHA-256 over the
canonical supported evidence. Resource groups without `service.name` are not
owned service telemetry and are skipped; an envelope containing no supported
service record goes to `otlp.dlq` with source coordinates, a payload digest and
the reason, never the raw payload.

## Delivery and time rules

The consumer disables auto-commit. For each raw record it writes ClickHouse,
waits for every `obs.normalized` broker acknowledgement, updates its bounded
in-process dedup set, and only then commits the next raw offset. A transient
output failure retries the same record with capped backoff. ClickHouse's stable
ID and `ReplacingMergeTree` make a repeated insert logically idempotent.

Feature windows are fixed and half-open. Each `(service, signal)` stream owns
its watermark, so activity in one service cannot close another service's
window. Points behind the watermark and duplicates are counted and dropped;
accepted points are sorted by `(event time, observation ID)` and aggregated
with stable summation before a window closes.

## Verification

`make verify` runs deterministic unit and property tests. With `make lab-up`
and `make lab-deploy` running, `make verify-ingest` waits for real metric, log
and trace envelopes, passes one supported batch of each through the worker, and
proves representative IDs appear in both `obs.normalized` and ClickHouse.

The ingest container has a 384 MB hard limit and exposes no host port. Its
Kafka client includes the Zstandard codec because the Collector compresses raw
batches with Zstandard. Redpanda retains raw, normalized and DLQ topics for the
cluster's verified seven-day default.
