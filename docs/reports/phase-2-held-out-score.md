# Phase 2 per-symptom episode scoring proof

**Held-out gate: PASS**

- Telemetry evidence: **REAL** OpenTelemetry capture bytes replayed through the runtime detector and anti-flapping episode paths.
- Workload, event context and injected faults: **SIMULATED** and bounded to the contained Astronomy Shop testbed.
- Seed discipline: every capture below is a **HELD-OUT** `cascade_night` / `combo_night` seed, sealed and unused throughout development; detector parameters were frozen by development evidence before these seeds were recorded.
- Label discipline: public runtime replay completes before scorer-only private labels are opened. Predictions are matched one-to-one over half-open event-time intervals; retries of one `episode_id` count once.
- Runtime config fingerprint: `23d957af85fe1fa0cea7d8f4221c53ea571d1ffaa49a13de475de65f241d7030`.

## Held-out captures

| Capture | Profile | Seed | Kind | Predicted | Expected | TP | FP | FN | Precision | Recall | Detect p50/p95 |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| phase2-cascade-9403-v1 | cascade_night | 9403 | EDGE_DEGRADED | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 5.9s/5.9s |
| phase2-cascade-9421-v1 | cascade_night | 9421 | EDGE_DEGRADED | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 5.7s/5.7s |
| phase2-combo-9439-v1 | combo_night | 9439 | RESIDUAL_EXCEED | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 5.7s/5.7s |
| phase2-combo-9439-v1 | combo_night | 9439 | RATIO_DEFORM | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 175.7s/175.7s |
| phase2-combo-9439-v1 | combo_night | 9439 | LOG_BURST | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 172.4s/172.4s |
| phase2-combo-9439-v1 | combo_night | 9439 | EDGE_DEGRADED | 3 | 2 | 2 | 1 | 0 | 0.667 | 1.000 | 16.4s/16.4s |
| phase2-combo-9439-v1 | combo_night | 9439 | SATURATION | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 128.0s/128.0s |
| phase2-combo-9439-v1 | combo_night | 9439 | DROP | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 4.0s/4.0s |
| phase2-combo-9439-v1 | combo_night | 9439 | SILENCE | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 181.6s/181.6s |
| phase2-combo-9457-v1 | combo_night | 9457 | RESIDUAL_EXCEED | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 5.8s/5.8s |
| phase2-combo-9457-v1 | combo_night | 9457 | RATIO_DEFORM | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 175.8s/175.8s |
| phase2-combo-9457-v1 | combo_night | 9457 | LOG_BURST | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 172.0s/172.0s |
| phase2-combo-9457-v1 | combo_night | 9457 | EDGE_DEGRADED | 3 | 2 | 2 | 1 | 0 | 0.667 | 1.000 | 16.0s/16.0s |
| phase2-combo-9457-v1 | combo_night | 9457 | SATURATION | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 117.7s/117.7s |
| phase2-combo-9457-v1 | combo_night | 9457 | DROP | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 4.0s/4.0s |
| phase2-combo-9457-v1 | combo_night | 9457 | SILENCE | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 179.6s/179.6s |

## Per-kind held-out gates

Phase 2 gates on **recall/coverage** only: every labeled symptom must be caught. Precision is recorded for transparency but **not gated here** -- collapsing the topological storm one fault produces into a single incident (the precision/FP accounting) is Phase 4 causal-collapse's job. The precision floor column is the retained Phase-4 target.

| Kind | Recall | Floor | Result | Precision | Phase-4 target |
|---|---:|---:|---|---:|---:|
| RESIDUAL_EXCEED | 1.000 | >= 0.900 | PASS | 1.000 | >= 0.900 |
| RATIO_DEFORM | 1.000 | >= 0.900 | PASS | 1.000 | >= 0.900 |
| LOG_BURST | 1.000 | >= 0.900 | PASS | 1.000 | >= 0.900 |
| EDGE_DEGRADED | 1.000 | >= 0.900 | PASS | 0.800 | >= 0.500 |
| SATURATION | 1.000 | >= 0.900 | PASS | 1.000 | >= 0.900 |
| DROP | 1.000 | >= 0.900 | PASS | 1.000 | >= 0.900 |
| SILENCE | 1.000 | >= 0.900 | PASS | 1.000 | >= 0.900 |

## Scope of this proof

This held-out proof is the Phase 2 closure. It scores fresh `cascade_night` / `combo_night` seeds that stayed sealed throughout development, gating each kind on **recall/coverage** with the committed recall floors unchanged; precision is recorded but deferred to Phase 4 causal collapse. Detector parameters were frozen by development evidence (fingerprint above) before these seeds were recorded, and no floor was lowered from held-out results. A below-floor kind is reported honestly rather than hidden.
