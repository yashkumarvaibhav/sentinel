# Phase 2 — `cascade_night` real-telemetry development capture

**Capture acceptance: PASS (development evidence, not a held-out score)**

`cascade_night` keeps the frontend volume fully explained by a simulated match
while a contained payment failure degrades the checkout-to-payment edge. A
second bounded checkout journey after flag restoration supplies real recovery
evidence. This capture validates the edge, log and ingress-ratio development
materializers without claiming detector precision or recall.

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
- **Capture-time runtime config fingerprint:**
  `25b6b55fa226476b2d73a08dbdcde34ba0f117f9936a263d75325daeff89bec0`.
- **Current replay config fingerprint:**
  `0fadaa18ffb693544883740080362540de6b0dbdf502d269680edeabfa33cd49`.
  This differs only because the later log/ratio window policies and
  telemetry-to-logical service mappings are now versioned configuration; the
  immutable raw bytes and capture-time fingerprint remain pinned separately.

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
| Complete log windows | 2 × 4 configured logical services |
| Active log episodes after replay | 0 |
| Complete ingress-ratio windows | 2 × 2 configured frontend ratios |
| Active ratio episodes after replay | 0 |

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

The same label-free raw replay now materializes independent 60-second log
windows for cart, checkout, frontend and payment. The first complete window is
baseline warmup with 25, 18, 308 and 6 valid records respectively. The second
contains 148, 41, 767 and 82 records: cart, checkout and payment produce
deterministic `BREACH` results while frontend produces a sufficient `CLEAR`.
The strongest payment evidence is the measured 41-record `Payment request
failed. Invalid token` template at 0.6833 messages/s against a zero baseline
(relative deformation 68.3333). Each breach is only `PENDING`; the incomplete
24-second capture tail is not converted into a third tick, so no log episode is
fabricated or left active. These are development observations, not calibrated
precision or recall claims.

The frontend ingress-ratio replay also contains exactly two complete 60-second
windows. The 308-request first window warms a path-entropy baseline of
2.414886 bits and an inter-arrival CV baseline of 0.445111. The 767-request
second window is sufficient and unambiguous: path entropy is 2.259707
(relative deformation 0.064259, below the configured 0.3 trigger), while
inter-arrival CV rises to 0.576218 and therefore has zero lower-direction
deformation. Both produce `CLEAR`/`HELD`, leaving no active ratio episode. The
materializer counts distinct normalized ingress observations even when several
requests share one trace, and uses each observation ID as its evidence identity.

## Replay provenance and scope

- Raw replay SHA-256:
  `9d84fe76e6064d2c08380970f71ae9d5a6e1d26c8f7791dc89d8dd235211351d`.
- Decomposition transcript SHA-256:
  `e605ce2c3b74c61c32e353f3bb364e97402c245c807910c572ae473d60b8a1a1`.
- Edge-detector transcript SHA-256:
  `9f3e364f4c8bda8aea08ecae2a017be245028fc4dc71137fc57dcb953dcf5c76`.
- Log-detector transcript SHA-256:
  `cc2d325c810da433b075fcb405c452d9a260aad6b7a3eaf2bedf2f524fb45cc1`.
- Ingress-ratio transcript SHA-256:
  `f6b6006e6f48529eb95fcc63d74af6477a72afabeb40a2c2eeb14c2168fa4d26`.
- The raw capture stays outside git because it is a DVC-scale binary artifact.
  The committed report pins the evidence needed to shape the deterministic
  window materializer.
- The earlier v5 development capture remains a useful insufficient-recovery
  oracle, but v6 supersedes it for lifecycle acceptance.
- This is deliberately not included in `make score`: Phase 2 per-symptom
  detector scoring is the next layer, and its final gates must use untouched
  held-out seeds.
