# Phase 5 — remediation loop replay (characterization)

**SIMULATED.** Every rung below was carried out by a stand-in adapter that changed nothing: no cluster, no proxy and no flag provider was contacted. The decisions are real - produced by the deterministic detection and decision planes from recorded telemetry - and the loop's choices are real. What was *done* about them was not.

There is no production `SloReader` until Phase 8, so every collateral probe reports its services as unreadable and every action is consequently undone. That is the honest behaviour - a protected service nobody can read is never one known to be fine - and it is why `undone` below equals `acted`.

## `phase2-cascade-401-dev-v6` — cascade_night, seed 401 (development)

- Acting decisions considered: **0**
- Passes that acted: **0**; that took no action: **0**
- Actions undone by the collateral probe: **0**
- Restraints given back on expiry: **0**
- Ledger entries written: **0** (head `000000000000…`)
- Ladders `19d2b7bccf88…` · detectors `23d957af85fe…` · decisions `768c3460dcb2-078392fb04bb-82371c5b25c7-1d0c321a8781`

## `phase2-combo-503-dev-v11` — combo_night, seed 503 (development)

- Acting decisions considered: **98**
- Passes that acted: **0**; that took no action: **98**
- Actions undone by the collateral probe: **0**
- Restraints given back on expiry: **0**
- Ledger entries written: **294** (head `0dad1a546e34…`)
- Ladders `19d2b7bccf88…` · detectors `23d957af85fe…` · decisions `768c3460dcb2-078392fb04bb-82371c5b25c7-1d0c321a8781`

Why passes declined, by reason:

- 98x hold-the-cohort-to-its-ceiling: RATE_LIMIT on simulated/frontend needs 1 approver(s)
- 98x add-headroom-for-the-surge: SCALE on simulated/frontend needs 1 approver(s)
