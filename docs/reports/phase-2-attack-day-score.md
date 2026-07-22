# Phase 2 — `attack_day` no-event control on real telemetry

**Gate: PASS (control scenario)**

`attack_day` is the pure-attack control profile (a volume surge with **no event on
the calendar**). It proves detection is not context-dependent: because nothing
explains the volume, the whole surge is unexplained residual. This report scores
two **held-out** seeds against **real** OpenTelemetry ingress spans captured from
the contained Astronomy Shop testbed — upgrading the Phase 2.8a deterministic
evaluator oracle to real measured telemetry.

- **Telemetry evidence: REAL.** Real `frontend-proxy` ingress spans recorded at
  exact raw-topic offsets from the live k3d mesh, re-normalized during bit-exact
  replay. Not scripted.
- **Workload + context: SIMULATED.** Deterministic, seed-controlled k6 load capped
  at ≤ 50 requests/s inside the testbed namespace; the profile carries **no**
  context window.
- **Seed discipline: HELD-OUT.** Seeds 9043 / 9067 are the committed `attack_day`
  held-out set, disjoint from its development seeds (301 / 313).
- **Label discipline.** The engine receives only `Observation`; the residual
  interval `attack_core` (offsets 64–84 s) is applied afterward by the scorer.
- **Runtime config fingerprint:** `24d700c64d5df9ac214c62cc196d95d4d2a6a58d0aac14bb3197350d3745879a`.

## Held-out runs

| Profile | Seed | Real spans | Complete | Ticks | TP | FP | FN | TN | Precision | Recall | FP rate | Detect p50/p95 | Replay hash |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| attack_day | 9043 | 576/576 | 1.000 | 12 | 10 | 0 | 0 | 2 | 1.000 | 1.000 | 0.000 | 0.0s/0.0s | `96ecc741…5724` |
| attack_day | 9067 | 576/576 | 1.000 | 12 | 10 | 0 | 0 | 2 | 1.000 | 1.000 | 0.000 | 0.0s/0.0s | `2e602292…10ca` |

## What this proves

The same surge shape that `match_night`'s event explains away (residual ≈ 0) is,
with no event present, flagged in full: every one of the 20 s attack window's ten
2 s ticks is unexplained residual, with zero false positives on the warmup — on
**real** telemetry, not a synthetic fixture. Detection does not depend on an event
existing.

## Provenance and scope

- Captures: `phase2-attack-9043-v1` (59 raw records / 14,357,888 bytes) and
  `phase2-attack-9067-v1` (62 raw records / 16,375,833 bytes), recorded with
  `make capture PROFILE=attack_day SEED=<seed> PURPOSE=held_out`. Raw capture
  bytes stay out of git (DVC-scale binary); the deterministic replay hash above
  pins each run's decomposition semantics.
- These captures are **not yet folded into the hosted `make score` DVC gate.**
  Doing so requires republishing the pinned `phase-1-captures-v1` bootstrap
  Release asset that hosted CI pulls from, plus extending the capture-matrix
  guard — a deliberate publishing step tracked in `BUILD_STATE.md`. This report is
  the committed measured evidence in the meantime.
- Scope: this scores residual extraction only (the control's single labeled
  window). Per-symptom-kind scoring across `cascade_night` / `combo_night` remains
  the rest of Phase 2.8, which needs the fault-driving harness and richer captures.
