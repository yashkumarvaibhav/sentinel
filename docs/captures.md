# Capture and replay boundary

Sentinel records decision-path input before interpretation. A capture manifest
names the exact inclusive-start/exclusive-end offset range for each raw OTLP
topic partition, then lists every payload in that range with its byte length,
relative path and SHA-256 checksum. Offsets must be contiguous. Missing,
duplicated, reordered, path-traversing or corrupt records fail before replay.

Each manifest also records the compiled public schedule, capture-scoped
telemetry routing, live scenario identity and config fingerprint, with explicit
honesty labels: telemetry is `REAL`; the bounded workload and event context are
`SIMULATED`. Public, capture-scoped context/enrichment snapshots have
independent checksums. The private labels path and checksum are declared, but
the runtime loader never opens that file; only the scoring loader can verify
and read it.

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
make capture PROFILE=match_night SEED=8923 CAPTURE_ID=phase1-match-8923-v2
make capture PROFILE=match_night SEED=211 PURPOSE=development \
  CAPTURE_ID=phase1-match-211-golden-v1
```

The ignored runtime object lands in `var/captures/<capture-id>`. Alongside the
raw payloads it contains checksummed schedule/context inputs, an explicit empty
Phase-1 enrichment snapshot and private scorer-only labels. DVC publication is
a separate reviewed step; recording never silently commits large telemetry.
The default purpose is `held_out`; golden captures must explicitly use a
committed `development` seed so reviewed regression fixtures never consume the
held-out scoring set.

`make replay-capture CAPTURE_ID=<capture-id>` verifies every checksum, reruns
the sole OTLP normalizer, selects the captured correlation and marker spans,
reconstructs fixed event-time rate ticks, materializes the captured relative
contexts and runs the deterministic decomposition engine. It writes canonical
JSON under ignored `var/replays/`; the transcript contains both capture-time
and replay-time config fingerprints, all rate observations, every warmup or
decomposition result, exact frames, raw-DLQ outcomes and honesty labels. It
does not load private labels.

The first real proof (`phase1-match-8923-v2`) captured 69 contiguous raw-topic
records / 20,810,307 bytes. Two independent loads and full replays produced the
same transcript SHA-256 (`ad32e4cd86f5ff0547e5cc7250afd5167549431258c657af06ba18bde5c8960f`):
712/712 correlated ingress spans, 50 ticks, 30 warmup ticks, 20 frames, 18 with
event context, 8 residual, and zero raw DLQ outcomes.

This completes the live-recorder and full-decomposition-transcript parts of
Phase 1.9. The next part adds DVC object pointers, then switches hosted
score/golden CI gates to those recorded inputs.
