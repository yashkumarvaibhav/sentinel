# Capture and replay boundary

Sentinel records decision-path input before interpretation. A capture manifest
names the exact inclusive-start/exclusive-end offset range for each raw OTLP
topic partition, then lists every payload in that range with its byte length,
relative path and SHA-256 checksum. Offsets must be contiguous. Missing,
duplicated, reordered, path-traversing or corrupt records fail before replay.

Each manifest also records the compiled public schedule, live scenario identity and config fingerprint,
with explicit honesty labels: telemetry is `REAL`; the bounded workload and
event context are `SIMULATED`. Public, capture-scoped context/enrichment
snapshots have independent checksums. The private labels path and checksum are
declared, but the runtime loader never opens that file; only the scoring loader
can verify and read it.

Raw replay sorts by source coordinates, re-runs the sole Phase 1.5 OTLP
normalizer, applies the same stable observation-ID dedup boundary and records
deterministic DLQ outcomes. Its canonical JSON transcript is byte-identical for
the same verified capture. Unit oracles corrupt raw bytes and labels
independently to prove that runtime evidence fails closed while private labels
remain physically isolated.

## Recording one live profile

The recorder never publishes Kafka outside the isolated Docker network. Two
short-lived clients join `sentinel_net`: the first snapshots every partition's
high watermark immediately before the owned k6 Job; the second snapshots the
ends after the scenario evidence reaches ClickHouse, then seeks to and drains
every offset in each frozen half-open range. Partition drift, watermark rewind,
offset gaps and null payloads fail closed.

```bash
make capture PROFILE=match_night SEED=8923 CAPTURE_ID=phase1-match-8923-v1
```

The ignored runtime object lands in `var/captures/<capture-id>`. Alongside the
raw payloads it contains checksummed schedule/context inputs, an explicit empty
Phase-1 enrichment snapshot and private scorer-only labels. DVC publication is
a separate reviewed step; recording never silently commits large telemetry.

This is the live recorder part of Phase 1.9. The next part adds full
decomposition transcripts and DVC object pointers,
then switches hosted score/golden CI gates to those recorded inputs.
