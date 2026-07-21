# Phase 1 decomposition scoring proof

**Gate: PASS**

- Telemetry evidence: **REAL** OpenTelemetry ingress spans from the contained Astronomy Shop testbed, read back from ClickHouse after Collector -> Redpanda -> ingest normalization.
- Workload and event context: **SIMULATED**, deterministic, seed-controlled and capped at 50 requests/s inside the testbed namespace.
- Seed discipline: every row below is from the committed **HELD-OUT** set; development tests use disjoint seeds.
- Label discipline: the engine receives only `Observation` plus `ContextWindow`; private residual intervals are applied afterward by `lab/scoring`.
- Runtime config fingerprint: `fdd7c78ffa81d5ba94fa19851bd38369c778d216ebf76488a4d8ab41c2a8e1d4`.

## Held-out runs

| Profile | Seed | Real spans | Complete | Ticks | TP | FP | FN | TN | Precision | Recall | FP rate | Detect p50/p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| quiet_day | 7901 | 336/336 | 1.000 | 12 | 0 | 0 | 0 | 12 | insufficient | insufficient | 0.000 | insufficient |
| quiet_day | 7919 | 336/336 | 1.000 | 12 | 0 | 0 | 0 | 12 | insufficient | insufficient | 0.000 | insufficient |
| match_night | 8923 | 712/712 | 1.000 | 20 | 8 | 0 | 0 | 12 | 1.000 | 1.000 | 0.000 | 0.0s/0.0s |
| match_night | 8941 | 712/712 | 1.000 | 20 | 8 | 0 | 0 | 12 | 1.000 | 1.000 | 0.000 | 0.0s/0.0s |

## Gate summary

| Metric | Actual | Requirement | Result |
|---|---:|---:|---|
| Residual precision | 1.000 | >= 0.900 | PASS |
| Residual recall | 1.000 | >= 0.900 | PASS |
| Quiet-day false-positive rate | 0.000 | <= 0.000 | PASS |
| Per-run telemetry completeness | 1.000 | >= 0.950 | PASS |

## Scope of this proof

This Phase 1 gate scores residual extraction only: event-explained volume, injected unexplained offset, and quiet-day false positives. Decision, reason, origin, action, calibration and resolution metrics remain `insufficient` until their owning phases land; no zeroes are fabricated for them.
