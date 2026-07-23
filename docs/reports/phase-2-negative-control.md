# Phase 2 episode characterization (label-free evidence)

Every durable episode each deterministic detector emits on the captures below, collapsed to its latest revision. **No private label is read** — this is the prediction side only, the evidence base for the recall/coverage gate and the fuller a-priori storm labels (BUILD_STATE 6a). It gates nothing on its own.

- Runtime config fingerprint: `23d957af85fe1fa0cea7d8f4221c53ea571d1ffaa49a13de475de65f241d7030`.
- Offsets are seconds from each capture's anchor; bit-exact replay keeps them stable.
- Telemetry is **REAL**; the injected context/fault/attack stimuli are **SIMULATED**.
- `episode_id` is shown as a 12-char prefix of the deterministic identity hash.

## Negative control: PASS

Anti-spam guard (i): every fault kind (RATIO_DEFORM, LOG_BURST, EDGE_DEGRADED, SATURATION, DROP, SILENCE) must emit **zero** episodes on these no-fault captures. RESIDUAL_EXCEED is exempt (a legitimate signal on event-explained and attack traffic).

No fault-kind episode was emitted on any no-fault capture.

## Topology (config/topology.yml)

Direct downstream dependencies, so propagated symptoms below can be traced to a fault target by hand.

- `frontend` (edge/critical) -> `checkout`, `cart`
- `checkout` (application/critical) -> `cart`, `payment`, `email`
- `cart` (application/high) -> —
- `payment` (application/critical) -> —
- `email` (application/high) -> —

## phase1-quiet-101-golden-v1

- Scenario `quiet_day`, seed 101 (development).
- Window: anchor .. +84.0s.
- Episodes emitted: 0 total. Per kind: RESIDUAL_EXCEED 0 · RATIO_DEFORM 0 · LOG_BURST 0 · EDGE_DEGRADED 0 · SATURATION 0 · DROP 0 · SILENCE 0.

No episodes emitted.

## phase1-match-211-golden-v1

- Scenario `match_night`, seed 211 (development).
- Window: anchor .. +100.0s.
- Episodes emitted: 1 total. Per kind: RESIDUAL_EXCEED 1 · RATIO_DEFORM 0 · LOG_BURST 0 · EDGE_DEGRADED 0 · SATURATION 0 · DROP 0 · SILENCE 0.

| kind | service | signal | opened(s) | confirmed(s) | closed(s) | status | peak | breaches | episode_id |
|---|---|---|---|---|---|---|---|---|---|
| RESIDUAL_EXCEED | frontend | request_rate | 84.0 | 88.0 | active | ACTIVE | 1.000 | 8 | `8fcfb9c89f33` |

## phase2-attack-9043-v1

- Scenario `attack_day`, seed 9043 (held_out).
- Window: anchor .. +84.0s.
- Episodes emitted: 1 total. Per kind: RESIDUAL_EXCEED 1 · RATIO_DEFORM 0 · LOG_BURST 0 · EDGE_DEGRADED 0 · SATURATION 0 · DROP 0 · SILENCE 0.

| kind | service | signal | opened(s) | confirmed(s) | closed(s) | status | peak | breaches | episode_id |
|---|---|---|---|---|---|---|---|---|---|
| RESIDUAL_EXCEED | frontend | request_rate | 64.0 | 68.0 | active | ACTIVE | 1.000 | 10 | `1618e03e6892` |
