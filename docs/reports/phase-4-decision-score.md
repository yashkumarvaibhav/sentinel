# Phase 4 decision score

What the decision plane concluded, graded against each scenario's committed answer key. A decision label owns no interval of its own: it points at the residual and symptom labels it is the consequence of, so every window below inherits the **measured** offsets those labels were materialized to when the capture was recorded.

- **Gate: PASS**
- Runtime detector config fingerprint: `23d957af85fe1fa0cea7d8f4221c53ea571d1ffaa49a13de475de65f241d7030`.
- Decision config fingerprint (agents-rules-incidents-policy): `768c3460dcb2-078392fb04bb-82371c5b25c7-1d0c321a8781`.
- Required handling is stated over *surfacing* and *acting*, never over a policy rung: the ladder is operator-owned data, and retuning it must not read as a regression of the platform.
- Telemetry is **REAL**; the injected context/fault/attack stimuli are **SIMULATED**.
- **Development captures only.** These are the numbers the metric definitions and floors are
  frozen on, before any held-out seed is spent. This report gates nothing on its own.
- ⚠️ **`phase2-combo-503-dev-v11` predates the 6d-3 anchored DROP label** (`14383d1`), so its
  `frontend-measured-rate-drop` label covers 713..724s instead of the 2-rps phase the primary
  schedule actually ran over 664..724s.

## Gate

| Metric | Value | Floor |
|---|---:|---:|
| decision accuracy | 1.000 (7/7) | — |
| decision+reason accuracy | 0.857 (6/7) | — |
| attack recall | 1.000 (1/1) | — |
| origin accuracy | 1.000 (5/5) | — |
| **false acts** | **0** | **0** |

## phase1-quiet-101-golden-v1

- Scenario `quiet_day`, seed 101 (development); 0 decisions over 0 labelled windows.

No decision window is labelled for this scenario, so nothing is expected of the platform here except restraint: **0 false acts**.

## phase1-match-211-golden-v1

- Scenario `match_night`, seed 211 (development); 7 decisions over 1 labelled windows.

| window | expects | origin | surfaced | acted | named | handled | reason | origin ok |
|---|---|---|---|---|---|---|---|---|
| `unexplained-offset-decision` (84..100s) | UNEXPLAINED | — | yes | no | — | ok | ok | — |

## phase2-cascade-401-dev-v6

- Scenario `cascade_night`, seed 401 (development); 5 decisions over 1 labelled windows.

| window | expects | origin | surfaced | acted | named | handled | reason | origin ok |
|---|---|---|---|---|---|---|---|---|
| `payment-fault-decision` (89..129s) | OPERATIONAL_FAULT | payment | yes | no | OPERATIONAL_FAULT | ok | ok | ok |

Windows narrowed by evidence this capture predates (the question is weaker here, and the report says so rather than absorbing it):

- `payment-fault-decision` is missing `frontend-checkout-propagation`

## phase2-combo-503-dev-v11

- Scenario `combo_night`, seed 503 (development); 501 decisions over 5 labelled windows.

| window | expects | origin | surfaced | acted | named | handled | reason | origin ok |
|---|---|---|---|---|---|---|---|---|
| `behavior-attack-decision` (143..329s) | ATTACK | frontend | yes | yes | ATTACK | ok | ok | ok |
| `payment-fault-decision` (333..521s) | OPERATIONAL_FAULT | payment | yes | no | OPERATIONAL_FAULT | ok | ok | ok |
| `email-saturation-decision` (525..713s) | OPERATIONAL_FAULT | email | yes | no | OPERATIONAL_FAULT | ok | ok | ok |
| `measured-drop-decision` (713..724s) | UNEXPLAINED | — | yes | no | OPERATIONAL_FAULT | ok | MISS | — |
| `emitter-silence-decision` (724..944s) | OPERATIONAL_FAULT | frontend | yes | no | EXPECTED_EVENT+OPERATIONAL_FAULT | ok | ok | ok |

Windows narrowed by evidence this capture predates (the question is weaker here, and the report says so rather than absorbing it):

- `payment-fault-decision` is missing `frontend-checkout-propagation`
