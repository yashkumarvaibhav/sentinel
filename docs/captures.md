# Capture and replay boundary

Sentinel records decision-path input before interpretation. A capture manifest
names the exact inclusive-start/exclusive-end offset range for each raw OTLP
topic partition, then lists every payload in that range with its byte length,
relative path and SHA-256 checksum. Offsets must be contiguous. Missing,
duplicated, reordered, path-traversing or corrupt records fail before replay.

Each manifest also records the live scenario identity and config fingerprint,
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

This is the first bounded part of Phase 1.9. The next part records the live
profile ranges, adds full decomposition transcripts and DVC object pointers,
then switches hosted score/golden CI gates to those recorded inputs.
