# Phase 2 episode characterization (label-free evidence)

Every durable episode each deterministic detector emits on the captures below, collapsed to its latest revision. **No private label is read** — this is the prediction side only, the evidence base for the recall/coverage gate and the fuller a-priori storm labels (BUILD_STATE 6a). It gates nothing.

- Runtime config fingerprint: `23d957af85fe1fa0cea7d8f4221c53ea571d1ffaa49a13de475de65f241d7030`.
- Offsets are seconds from each capture's anchor; bit-exact replay keeps them stable.
- Telemetry is **REAL**; the injected context/fault/attack stimuli are **SIMULATED**.
- `episode_id` is shown as a 12-char prefix of the deterministic identity hash.

## Topology (config/topology.yml)

Direct downstream dependencies, so propagated symptoms below can be traced to a fault target by hand.

- `frontend` (edge/critical) -> `checkout`, `cart`
- `checkout` (application/critical) -> `cart`, `payment`, `email`
- `cart` (application/high) -> —
- `payment` (application/critical) -> —
- `email` (application/high) -> —

## phase2-cascade-401-dev-v6

- Scenario `cascade_night`, seed 401 (development).
- Window: anchor .. +144.0s.
- Episodes emitted: 2 total. Per kind: RESIDUAL_EXCEED 0 · RATIO_DEFORM 0 · LOG_BURST 0 · EDGE_DEGRADED 2 · SATURATION 0 · DROP 0 · SILENCE 0.

| kind | service | signal | opened(s) | confirmed(s) | closed(s) | status | peak | breaches | episode_id |
|---|---|---|---|---|---|---|---|---|---|
| EDGE_DEGRADED | frontend | dependency.checkout | 100.0 | 104.0 | 144.0 | CLOSED | 1.000 | 6 | `2dd34b7f8079` |
| EDGE_DEGRADED | checkout | dependency.payment | 100.0 | 104.0 | 144.0 | CLOSED | 1.000 | 6 | `536616bc3805` |

## phase2-combo-503-dev-v11

- Scenario `combo_night`, seed 503 (development).
- Window: anchor .. +1004.0s.
- Episodes emitted: 8 total. Per kind: RESIDUAL_EXCEED 1 · RATIO_DEFORM 1 · LOG_BURST 1 · EDGE_DEGRADED 2 · SATURATION 1 · DROP 1 · SILENCE 1.

| kind | service | signal | opened(s) | confirmed(s) | closed(s) | status | peak | breaches | episode_id |
|---|---|---|---|---|---|---|---|---|---|
| DROP | frontend | request_rate | 664.0 | 668.0 | 728.0 | CLOSED | 0.889 | 30 | `f727a39586d4` |
| EDGE_DEGRADED | frontend | dependency.checkout | 344.0 | 348.0 | 540.0 | CLOSED | 1.000 | 86 | `60fed498a0fb` |
| EDGE_DEGRADED | checkout | dependency.payment | 344.0 | 348.0 | 540.0 | CLOSED | 1.000 | 86 | `f295d71d481d` |
| LOG_BURST | payment | log_template_rate | 360.0 | 480.0 | 720.0 | CLOSED | 1.000 | 4 | `53035f8a414c` |
| RATIO_DEFORM | frontend | path_entropy | 180.0 | 300.0 | 540.0 | CLOSED | 1.000 | 3 | `10a22dc0180e` |
| RESIDUAL_EXCEED | frontend | request_rate | 144.0 | 148.0 | 330.0 | CLOSED | 1.000 | 91 | `b85760053bb9` |
| SATURATION | email | container_memory | 600.0 | 610.0 | 690.0 | CLOSED | 1.000 | 6 | `952f0de49eab` |
| SILENCE | frontend | request_rate | 902.0 | 904.0 | 946.0 | CLOSED | 0.633 | 21 | `934e196272b5` |
