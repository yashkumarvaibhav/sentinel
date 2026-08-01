# Phase 6 live-run score

What the always-on producer **stored** while an authored scenario ran on the testbed, graded against that scenario's committed answer key. Every scorer before this one replays a recording and grades the transcript; this one grades the rows an operator would actually have been looking at.

- **Gate: FAIL**
- Run `phase6-live-combo-509-v1`: scenario `combo_night`, seed 509 (development).
- Anchor `2026-08-01T07:01:08.673551+00:00` .. `2026-08-01T07:17:52.673551+00:00`.
- Producer `live-producer` was anchored at `2026-08-01T06:28:10+00:00` and durably reached `2026-08-01T09:23:04+00:00`, so it observed the whole run.
- 6 of 6 stored incidents overlapped the run; the rest belong to other traffic and are not graded.
- The store keeps the **current state** of each incident, not its trajectory, so this grades the last thing the platform said about each one. That is a weaker question than a replay transcript answers, and it is the one the product puts in front of a person.
- Telemetry is **REAL**; the injected context/fault/attack stimuli are **SIMULATED**.

⚠ The board was **not clean**: 2 incident(s) were already open when the run began (`8facb3855b7b`, `af2775b576cf`). They are graded, because they were genuinely on the operator's board and answering for services this run asks about - but the questions below were asked over a busier board than a clean start would have given, and that is part of the result rather than an excuse for it.

## Gate

| Metric | Value | Floor |
|---|---:|---:|
| decision accuracy | 0.800 (4/5) | — |
| decision+reason accuracy | 0.400 (2/5) | — |
| attack recall | 0.000 (0/1) | — |
| origin accuracy | 0.750 (3/4) | — |
| **false acts** | **0** | **0** |

Failures:

- `decision_accuracy` (overall): 0.800 needs >=0.900
- `decision_reason_accuracy` (overall): 0.400 needs >=0.850
- `attack_recall` (overall): 0.000 needs >=0.900
- `origin_accuracy` (overall): 0.750 needs >=0.800

## Windows

| window | expects | origin | surfaced | acted | named | handled | reason | origin ok |
|---|---|---|---|---|---|---|---|---|
| `behavior-attack-decision` (124..304s) | ATTACK | frontend | yes | no | OPERATIONAL_FAULT | ok | MISS | ok |
| `payment-fault-decision` (310..488s) | OPERATIONAL_FAULT | payment | yes | no | OPERATIONAL_FAULT | ok | ok | ok |
| `email-saturation-decision` (492..668s) | OPERATIONAL_FAULT | email | NO | no | — | MISS | MISS | MISS |
| `measured-drop-decision` (664..724s) | UNEXPLAINED | — | yes | no | OPERATIONAL_FAULT | ok | MISS | — |
| `emitter-silence-decision` (724..944s) | OPERATIONAL_FAULT | frontend | yes | no | OPERATIONAL_FAULT | ok | ok | ok |
