# Phase 2 episode characterization (label-free evidence)

Every durable episode each deterministic detector emits on the captures below, collapsed to its latest revision. **No private label is read** — this is the prediction side only, the evidence base for the recall/coverage gate and the fuller a-priori storm labels (BUILD_STATE 6a). It gates nothing on its own.

- Runtime config fingerprint: `4717fe4bceca528b14c81320069e421d6891baea2b4bc8363a8585b320cea70a`.
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

## phase6-live-combo-509-v1

- Scenario `combo_night`, seed 509 (development).
- Window: anchor .. +1004.0s.
- Episodes emitted: 11 total. Per kind: RESIDUAL_EXCEED 1 · RATIO_DEFORM 1 · LOG_BURST 2 · EDGE_DEGRADED 4 · SATURATION 1 · DROP 1 · SILENCE 1.

| kind | service | signal | opened(s) | confirmed(s) | closed(s) | status | peak | breaches | episode_id |
|---|---|---|---|---|---|---|---|---|---|
| DROP | frontend | request_rate | 664.0 | 668.0 | 728.0 | CLOSED | 0.889 | 30 | `c63bdd6f0a53` |
| EDGE_DEGRADED | frontend | dependency.checkout | 322.0 | 326.0 | 524.0 | CLOSED | 1.000 | 99 | `4fe719c5e773` |
| EDGE_DEGRADED | checkout | dependency.payment | 322.0 | 326.0 | 504.0 | CLOSED | 1.000 | 89 | `c9d6973551c5` |
| EDGE_DEGRADED | frontend | dependency.checkout | 642.0 | 646.0 | active | ACTIVE | 1.000 | 17 | `7d462811091b` |
| EDGE_DEGRADED | checkout | dependency.cart | 670.0 | 674.0 | active | ACTIVE | 1.000 | 5 | `388fc0d12d53` |
| LOG_BURST | payment | log_template_rate | 360.0 | 480.0 | 720.0 | CLOSED | 1.000 | 4 | `8ffa11f7173c` |
| LOG_BURST | frontend | log_template_rate | 540.0 | 660.0 | active | ACTIVE | 1.000 | 6 | `1194b1f8afca` |
| RATIO_DEFORM | frontend | path_entropy | 180.0 | 300.0 | 480.0 | CLOSED | 1.000 | 3 | `40af8def8c92` |
| RESIDUAL_EXCEED | frontend | request_rate | 126.0 | 130.0 | 310.0 | CLOSED | 1.000 | 90 | `9e5611c75dc5` |
| SATURATION | email | container_memory | 560.0 | 570.0 | 710.0 | CLOSED | 1.000 | 12 | `ba3cbe50654d` |
| SILENCE | frontend | request_rate | 902.0 | 904.0 | 944.0 | CLOSED | 0.627 | 20 | `28e54e0489bc` |
