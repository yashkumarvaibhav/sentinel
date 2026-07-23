# Phase 2 per-symptom episode scoring proof

**Held-out gate: FAIL**

- Telemetry evidence: **REAL** OpenTelemetry capture bytes replayed through the runtime detector and anti-flapping episode paths.
- Workload, event context and injected faults: **SIMULATED** and bounded to the contained Astronomy Shop testbed.
- Seed discipline: every capture below is a **HELD-OUT** `cascade_night` / `combo_night` seed, sealed and unused throughout development; detector parameters were frozen by development evidence before these seeds were recorded.
- Label discipline: public runtime replay completes before scorer-only private labels are opened. Predictions are matched one-to-one over half-open event-time intervals; retries of one `episode_id` count once.
- Runtime config fingerprint: `23d957af85fe1fa0cea7d8f4221c53ea571d1ffaa49a13de475de65f241d7030`.

## Held-out captures

| Capture | Profile | Seed | Kind | Predicted | Expected | TP | FP | FN | Precision | Recall | Detect p50/p95 |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| phase2-cascade-9103-v2 | cascade_night | 9103 | EDGE_DEGRADED | 2 | 1 | 1 | 1 | 0 | 0.500 | 1.000 | 6.2s/6.2s |
| phase2-cascade-9127-v1 | cascade_night | 9127 | EDGE_DEGRADED | 2 | 1 | 1 | 1 | 0 | 0.500 | 1.000 | 6.3s/6.3s |
| phase2-combo-9209-v1 | combo_night | 9209 | RESIDUAL_EXCEED | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 5.0s/5.0s |
| phase2-combo-9209-v1 | combo_night | 9209 | RATIO_DEFORM | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 155.0s/155.0s |
| phase2-combo-9209-v1 | combo_night | 9209 | LOG_BURST | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 145.1s/145.1s |
| phase2-combo-9209-v1 | combo_night | 9209 | EDGE_DEGRADED | 3 | 1 | 1 | 2 | 0 | 0.333 | 1.000 | 15.1s/15.1s |
| phase2-combo-9209-v1 | combo_night | 9209 | SATURATION | 2 | 1 | 1 | 1 | 0 | 0.500 | 1.000 | 82.7s/82.7s |
| phase2-combo-9209-v1 | combo_night | 9209 | DROP | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 0.0s/0.0s |
| phase2-combo-9209-v1 | combo_night | 9209 | SILENCE | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 179.6s/179.6s |
| phase2-combo-9221-v1 | combo_night | 9221 | RESIDUAL_EXCEED | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 5.1s/5.1s |
| phase2-combo-9221-v1 | combo_night | 9221 | RATIO_DEFORM | 3 | 1 | 1 | 2 | 0 | 0.333 | 1.000 | 159.1s/159.1s |
| phase2-combo-9221-v1 | combo_night | 9221 | LOG_BURST | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 149.5s/149.5s |
| phase2-combo-9221-v1 | combo_night | 9221 | EDGE_DEGRADED | 3 | 1 | 1 | 2 | 0 | 0.333 | 1.000 | 15.5s/15.5s |
| phase2-combo-9221-v1 | combo_night | 9221 | SATURATION | 0 | 1 | 0 | 0 | 1 | insufficient | 0.000 | insufficient |
| phase2-combo-9221-v1 | combo_night | 9221 | DROP | 1 | 1 | 0 | 1 | 1 | 0.000 | 0.000 | insufficient |
| phase2-combo-9221-v1 | combo_night | 9221 | SILENCE | 0 | 1 | 0 | 0 | 1 | insufficient | 0.000 | insufficient |

## Per-kind held-out gates

| Kind | Precision | Floor | Recall | Floor | Scope | Result |
|---|---:|---:|---:|---:|---|---|
| RESIDUAL_EXCEED | 1.000 | >= 0.900 | 1.000 | >= 0.900 | required | PASS |
| RATIO_DEFORM | 0.500 | >= 0.900 | 1.000 | >= 0.900 | required | FAIL |
| LOG_BURST | 1.000 | >= 0.900 | 1.000 | >= 0.900 | required | PASS |
| EDGE_DEGRADED | 0.400 | >= 0.500 | 1.000 | >= 0.900 | required | FAIL |
| SATURATION | 0.500 | >= 0.900 | 0.500 | >= 0.900 | required | FAIL |
| DROP | 0.500 | >= 0.900 | 0.500 | >= 0.900 | required | FAIL |
| SILENCE | 1.000 | >= 0.900 | 0.500 | >= 0.900 | required | FAIL |

## Failures

- `symptom_precision` for `RATIO_DEFORM`: 0.500; requires >=0.900.
- `symptom_precision` for `EDGE_DEGRADED`: 0.400; requires >=0.500.
- `symptom_precision` for `SATURATION`: 0.500; requires >=0.900.
- `symptom_recall` for `SATURATION`: 0.500; requires >=0.900.
- `symptom_precision` for `DROP`: 0.500; requires >=0.900.
- `symptom_recall` for `DROP`: 0.500; requires >=0.900.
- `symptom_recall` for `SILENCE`: 0.500; requires >=0.900.

## Scope of this proof

This held-out proof is the Phase 2 closure. It scores fresh `cascade_night` / `combo_night` seeds that stayed sealed throughout development, applying every committed per-kind floor unchanged. Detector parameters were frozen by development evidence (fingerprint above) before these seeds were recorded, and no floor was lowered from held-out results. A below-floor kind is reported honestly rather than hidden; `cascade_night` seeds carry only residual/edge labels, so kinds they do not exercise contribute their real predictions and any false positives to the pooled score.
