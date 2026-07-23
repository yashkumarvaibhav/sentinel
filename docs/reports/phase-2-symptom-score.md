# Phase 2 per-symptom episode scoring proof

**Development gate: PASS**

- Telemetry evidence: **REAL** OpenTelemetry capture bytes replayed through the runtime detector and anti-flapping episode paths.
- Workload, event context and injected faults: **SIMULATED** and bounded to the contained Astronomy Shop testbed.
- Seed discipline: this proof uses **DEVELOPMENT** captures only. It neither runs nor reads held-out `cascade_night` / `combo_night` seeds.
- Label discipline: public runtime replay completes before scorer-only private labels are opened. Predictions are matched one-to-one over half-open event-time intervals; retries of one `episode_id` count once.
- Runtime config fingerprint: `23d957af85fe1fa0cea7d8f4221c53ea571d1ffaa49a13de475de65f241d7030`.

## Development captures

| Capture | Profile | Seed | Kind | Predicted | Expected | TP | FP | FN | Precision | Recall | Detect p50/p95 |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| phase1-match-211-golden-v1 | match_night | 211 | RESIDUAL_EXCEED | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 4.0s/4.0s |
| phase2-cascade-401-dev-v6 | cascade_night | 401 | EDGE_DEGRADED | 2 | 1 | 1 | 1 | 0 | 0.500 | 1.000 | 15.4s/15.4s |
| phase2-combo-503-dev-v11 | combo_night | 503 | RESIDUAL_EXCEED | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 5.1s/5.1s |
| phase2-combo-503-dev-v11 | combo_night | 503 | RATIO_DEFORM | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 157.1s/157.1s |
| phase2-combo-503-dev-v11 | combo_night | 503 | LOG_BURST | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 147.5s/147.5s |
| phase2-combo-503-dev-v11 | combo_night | 503 | EDGE_DEGRADED | 2 | 1 | 1 | 1 | 0 | 0.500 | 1.000 | 15.5s/15.5s |
| phase2-combo-503-dev-v11 | combo_night | 503 | SATURATION | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 85.1s/85.1s |
| phase2-combo-503-dev-v11 | combo_night | 503 | DROP | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 0.0s/0.0s |
| phase2-combo-503-dev-v11 | combo_night | 503 | SILENCE | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 179.7s/179.7s |

## Per-kind development gates

| Kind | Precision | Floor | Recall | Floor | Scope | Result |
|---|---:|---:|---:|---:|---|---|
| RESIDUAL_EXCEED | 1.000 | >= 0.900 | 1.000 | >= 0.900 | required | PASS |
| RATIO_DEFORM | 1.000 | >= 0.900 | 1.000 | >= 0.900 | required | PASS |
| LOG_BURST | 1.000 | >= 0.900 | 1.000 | >= 0.900 | required | PASS |
| EDGE_DEGRADED | 0.500 | >= 0.500 | 1.000 | >= 0.900 | required | PASS |
| SATURATION | 1.000 | >= 0.900 | 1.000 | >= 0.900 | required | PASS |
| DROP | 1.000 | >= 0.900 | 1.000 | >= 0.900 | required | PASS |
| SILENCE | 1.000 | >= 0.900 | 1.000 | >= 0.900 | required | PASS |

## Scope of this proof

This development proof gates every kind listed above on measured development captures only. Kinds without an expected or predicted episode remain `insufficient`; they are not rendered as zero and are not release-gated until their development scenarios supply honest labels. The final Phase 2 gate still requires fresh held-out cascade/combo captures and every configured symptom kind.
