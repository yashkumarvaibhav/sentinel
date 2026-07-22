# Phase 2 — `cascade_night` real-telemetry development capture

**Capture acceptance: PASS (development evidence, not a held-out score)**

`cascade_night` keeps the frontend volume fully explained by a simulated match
while a contained payment failure degrades the checkout-to-payment edge. A
second bounded checkout journey after flag restoration supplies real recovery
evidence. This capture validates the first Phase 2 window-materializer vertical
slice without claiming detector precision or recall.

- **Telemetry evidence: REAL.** Logs, metrics and traces came from the contained
  Astronomy Shop testbed and were retained at exact raw-topic offsets.
- **Workload, context and fault: SIMULATED.** The primary k6 schedule, match
  context, fixed checkout journey and `paymentFailure=100%` flag mutation were
  bounded to the testbed.
- **Seed discipline: DEVELOPMENT.** Seed 401 is in the profile's development
  set. Neither held-out seed 9103 nor 9127 was executed or inspected.
- **Label discipline.** The public schedule records the requested plan and the
  public execution enrichment records measured UTC timestamps. The private
  `EDGE_DEGRADED` label was materialized afterward from the measured flag
  interval; runtime detection does not read it.
- **Runtime config fingerprint:**
  `25b6b55fa226476b2d73a08dbdcde34ba0f117f9936a263d75325daeff89bec0`.

## Capture evidence

| Evidence | Result |
|---|---:|
| Capture ID | `phase2-cascade-401-dev-v6` |
| Raw records / bytes | 196 / 39,471,051 |
| Normalized observations / services | 40,084 / 26 |
| Raw dead letters | 0 |
| Primary ingress completeness | 1,056 / 1,056 (1.000) |
| Decomposition ticks / positive residual ticks | 72 / 0 |
| Fault-journey ingress spans | 117 / 120 (0.975) |
| Fault-journey payment calls | 39 / 39 failed (gRPC status 2) |
| Recovery-journey ingress spans | 117 / 120 (0.975) |
| Recovery-journey payment calls | 39 / 39 successful (gRPC status 0) |
| Measured flag interval | 88.586017–128.686787 s |
| Private label interval | 88.586017–128.686787 s (exact match) |
| Edge episode lifecycle | `OPENED` +104 s; `CLOSED` +144 s |

The legitimate surge remains decomposed as `explained_base=4` plus
`explained_event=6` with residual 0. The trace evidence simultaneously contains
39 failed payment dependency calls from the fault journey and 39 successful
calls from the recovery journey. This is the intended context-aware cascade
shape: volume is explained, while behavior is independently verified from
dependency evidence.

The checkout→payment runner first produced six sufficient breach windows at
+100 through +110 seconds. Three consecutive breaches opened the episode at
+104 seconds. Sparse time between the two measured journeys remained explicitly
`INSUFFICIENT` and did not clear it. Only the three sufficient zero-error
windows at +140, +142 and +144 seconds produced `CLEARING`, `CLEARING`, then
`CLOSED`; no active edge episode remained after replay.

## Replay provenance and scope

- Raw replay SHA-256:
  `9d84fe76e6064d2c08380970f71ae9d5a6e1d26c8f7791dc89d8dd235211351d`.
- Decomposition transcript SHA-256:
  `b6622b4fee1406288a8ce9818bff91ad7962b5533c4d5bda58a650555a427107`.
- Edge-detector transcript SHA-256:
  `1d835634a9f1bbcb083ee974f4724a0d181980fb1fd04ddcf79a6073fd07b436`.
- The raw capture stays outside git because it is a DVC-scale binary artifact.
  The committed report pins the evidence needed to shape the deterministic
  window materializer.
- The earlier v5 development capture remains a useful insufficient-recovery
  oracle, but v6 supersedes it for lifecycle acceptance.
- This is deliberately not included in `make score`: Phase 2 per-symptom
  detector scoring is the next layer, and its final gates must use untouched
  held-out seeds.
