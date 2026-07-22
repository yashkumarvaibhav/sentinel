# Phase 2 per-symptom episode scoring proof

**Development gate: PASS**

- Telemetry evidence: **REAL** OpenTelemetry capture bytes replayed through the runtime detector and anti-flapping episode paths.
- Workload, event context and injected faults: **SIMULATED** and bounded to the contained Astronomy Shop testbed.
- Seed discipline: this proof uses **DEVELOPMENT** captures only. It neither runs nor reads held-out `cascade_night` / `combo_night` seeds.
- Label discipline: public runtime replay completes before scorer-only private labels are opened. Predictions are matched one-to-one over half-open event-time intervals; retries of one `episode_id` count once.
- Runtime config fingerprint: `29d0466ab1faec91c95a03daf1a6093e466aebdd2d220a654e29fa029fb19397`.

## Development captures

| Capture | Profile | Seed | Kind | Predicted | Expected | TP | FP | FN | Precision | Recall | Detect p50/p95 |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| phase1-match-211-golden-v1 | match_night | 211 | RESIDUAL_EXCEED | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 4.0s/4.0s |
| phase2-cascade-401-dev-v6 | cascade_night | 401 | EDGE_DEGRADED | 2 | 1 | 1 | 1 | 0 | 0.500 | 1.000 | 15.4s/15.4s |

## Per-kind development gates

| Kind | Precision | Floor | Recall | Floor | Scope | Result |
|---|---:|---:|---:|---:|---|---|
| RESIDUAL_EXCEED | 1.000 | >= 0.900 | 1.000 | >= 0.900 | required | PASS |
| RATIO_DEFORM | insufficient | >= 0.900 | insufficient | >= 0.900 | pending evidence | NOT GATED |
| LOG_BURST | insufficient | >= 0.900 | insufficient | >= 0.900 | pending evidence | NOT GATED |
| EDGE_DEGRADED | 0.500 | >= 0.500 | 1.000 | >= 0.900 | required | PASS |
| SATURATION | insufficient | >= 0.900 | insufficient | >= 0.900 | pending evidence | NOT GATED |
| DROP | insufficient | >= 0.900 | insufficient | >= 0.900 | pending evidence | NOT GATED |
| SILENCE | insufficient | >= 0.900 | insufficient | >= 0.900 | pending evidence | NOT GATED |

## Scope of this proof

This development slice freezes the generic evaluator and the first measured RESIDUAL_EXCEED / EDGE_DEGRADED baselines. Kinds without an expected or predicted episode remain `insufficient`; they are not rendered as zero and are not release-gated until their development scenarios supply honest labels. The final Phase 2 gate still requires fresh held-out cascade/combo captures and every configured symptom kind.
