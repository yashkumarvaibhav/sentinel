# Phase 2 — `cascade_night` real-telemetry development capture

**Capture acceptance: PASS (development evidence, not a held-out score)**

`cascade_night` keeps the frontend volume fully explained by a simulated match
while a contained payment failure degrades the checkout-to-payment edge. This
capture validates the multi-signal scenario shape needed by the Phase 2 window
materializer without claiming detector precision or recall.

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
  `24d700c64d5df9ac214c62cc196d95d4d2a6a58d0aac14bb3197350d3745879a`.

## Capture evidence

| Evidence | Result |
|---|---:|
| Capture ID | `phase2-cascade-401-dev-v5` |
| Raw records / bytes | 93 / 28,891,154 |
| Normalized observations / services | 30,993 / 27 |
| Raw dead letters | 0 |
| Primary ingress completeness | 1,056 / 1,056 (1.000) |
| Decomposition ticks / positive residual ticks | 72 / 0 |
| Checkout-journey ingress spans | 117 / 120 (0.975) |
| Correlated checkout-to-payment calls | 39 |
| Correlated failed payment calls | 39 (gRPC status 2) |
| Measured flag interval | 88.157018–124.277194 s |
| Private label interval | 88.157018–124.277194 s (exact match) |
| Clean recovery after flag restoration | 19.722806 s |

The legitimate surge remains decomposed as `explained_base=4` plus
`explained_event=6` with residual 0. The trace evidence simultaneously contains
39 failed payment dependency calls from the explicitly correlated checkout
journey. This is the intended context-aware cascade shape: volume is explained,
behavior is not silently inferred from volume, and the next detector layer can
consume measured dependency evidence.

## Replay provenance and scope

- Raw replay SHA-256:
  `14617fe1dcbd70f35797b482aa47b55bc35d89ef91e58e2f6a82b535363a8fe9`.
- Decomposition transcript SHA-256:
  `a37d925f07635fcfcf416f0de94dab1dde3e93d2e24f32bf7f40a7bab635c625`.
- The raw capture stays outside git because it is a DVC-scale binary artifact.
  The committed report pins the evidence needed to shape the deterministic
  window materializer.
- This is deliberately not included in `make score`: Phase 2 per-symptom
  detector scoring begins only after the window materializer and detector-run
  layer exist, and final gates must use the untouched held-out seeds.
