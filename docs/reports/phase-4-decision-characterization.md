# Phase 4 decision characterization (label-free evidence)

Every judgement the decision plane makes on the captures below: the four independent axis scores, the fused verdict, the incidents the storm collapsed into, the named origin and the four deterministic verification checks. **No private label is read.** This is the evidence the Phase-4 a-priori numbers are frozen on, before any held-out seed is spent. It gates nothing on its own.

- Runtime detector config fingerprint: `23d957af85fe1fa0cea7d8f4221c53ea571d1ffaa49a13de475de65f241d7030`.
- Decision config fingerprint (agents-rules-incidents-policy): `768c3460dcb2-078392fb04bb-82371c5b25c7-1d0c321a8781`.
- Offsets are seconds from each capture's anchor; the replay is deterministic.
- Telemetry is **REAL**; the injected context/fault/attack stimuli are **SIMULATED**.
- `CHG` is `—` on a capture: a capture records telemetry, not deploys, so the change axis reports insufficiency rather than a calm zero.
- A verdict column of `INSUFFICIENT` means evidence was measured and no signature accounted for it; `NO_EVIDENCE` means nothing contributed at all. They are different facts and lead to different decisions.
- `tick-wide` is what the whole mesh measured at that moment; `per-incident` is what each incident's **own** episodes support, and it is what the verdict, the memory signature and the decision are computed from. One incident's evidence can no longer lend a diagnosis to another.
- `decision` is the policy gate's answer per incident, and `approval` counts the incidents whose decision requires a person to sign off. A decision is taken on the incident's **peak** evidence with its **current** verification, so a storm that has gone quiet for one tick is still handled as the storm.

## phase1-quiet-101-golden-v1

- Scenario `quiet_day`, seed 101 (development).
- 0 durable episodes became 0 decision ticks over anchor .. +84.0s.
- Telemetry coverage: 24 services.

No durable episode was emitted, so the loop never ran.

## phase1-match-211-golden-v1

- Scenario `match_night`, seed 211 (development).
- 1 durable episodes became 7 decision ticks over anchor .. +100.0s.
- Telemetry coverage: 24 services.

Consecutive ticks whose judgement is identical are folded into one row, so every row below is a change in what the plane concluded.

| t(s) | ticks | SEC | REL | CHG | BIZ | tick-wide | per-incident | conf | incidents | origin | severity | state | verified | decision | approval |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|---|---|---|---|---|---:|
| 88..100 | 7 | 0.450 | 0.000 | — | 0.000 | INSUFFICIENT | INSUFFICIENT | — | 1 | frontend | LOW | OPEN | 1/1 | ALERT | 0/1 |

Final verification detail:

- Incident over `frontend`:
  - `temporal_causality`: **PASSED** — frontend was observed misbehaving first, no later than any of the 1 episodes it is credited with
  - `trace_coverage`: **PASSED** — all 1 named services were covered by telemetry
  - `dependency_validity`: **PASSED** — every named service and dependency edge exists in the committed topology
  - `memory_similarity`: **BOOTSTRAP** — incident memory holds 0 of the 20 entries this check needs; passed vacuously as a bootstrap, not as a confirmation

Final decision detail:

- Incident over `frontend`: **ALERT** (`tell-someone-what-we-cannot-name`, target `—`, evidence at +100s)
  - something is happening that no signature accounts for; that is a reason to tell a person, not a reason to relax

## phase2-cascade-401-dev-v6

- Scenario `cascade_night`, seed 401 (development).
- 2 durable episodes became 5 decision ticks over anchor .. +144.0s.
- Telemetry coverage: 26 services.

Consecutive ticks whose judgement is identical are folded into one row, so every row below is a change in what the plane concluded.

| t(s) | ticks | SEC | REL | CHG | BIZ | tick-wide | per-incident | conf | incidents | origin | severity | state | verified | decision | approval |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|---|---|---|---|---|---:|
| 104..110 | 4 | 0.000 | 0.978 | — | 0.000 | OPERATIONAL_FAULT | OPERATIONAL_FAULT | 0.647 | 1 | payment | LOW | OPEN | 1/1 | ESCALATE | 1/1 |
| 144 | 1 | 0.000 | 0.000 | — | 0.000 | NO_EVIDENCE | NO_EVIDENCE | — | 1 | payment | LOW | MONITORING | 1/1 | ESCALATE | 1/1 |

Final verification detail:

- Incident over `checkout, frontend`:
  - `temporal_causality`: **PASSED** — payment was observed misbehaving first, no later than any of the 2 episodes it is credited with
  - `trace_coverage`: **PASSED** — all 3 named services were covered by telemetry (including 1 implicated by a degraded edge)
  - `dependency_validity`: **PASSED** — every named service and dependency edge exists in the committed topology
  - `memory_similarity`: **BOOTSTRAP** — incident memory holds 0 of the 20 entries this check needs; passed vacuously as a bootstrap, not as a confirmation

Final decision detail:

- Incident over `checkout, frontend`: **ESCALATE_TO_HUMAN** (`hand-over-a-fault-we-cannot-confirm`, target `—`, evidence at +104s)
  - services are degrading but the hypothesis is unconfirmed or too weak to act on, so a person decides
  - approval: the ladder routes this to a person: services are degrading but the hypothesis is unconfirmed or too weak to act on, so a person decides

## phase2-combo-503-dev-v11

- Scenario `combo_night`, seed 503 (development).
- 8 durable episodes became 231 decision ticks over anchor .. +1004.0s.
- Telemetry coverage: 29 services.

Consecutive ticks whose judgement is identical are folded into one row, so every row below is a change in what the plane concluded.

| t(s) | ticks | SEC | REL | CHG | BIZ | tick-wide | per-incident | conf | incidents | origin | severity | state | verified | decision | approval |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|---|---|---|---|---|---:|
| 148..298 | 76 | 0.450 | 0.000 | — | 0.000 | INSUFFICIENT | INSUFFICIENT | — | 1 | frontend | LOW | OPEN | 1/1 | ALERT | 0/1 |
| 300..324 | 13 | 0.917 | 0.000 | — | 0.000 | ATTACK | ATTACK | 0.900 | 1 | frontend | LOW | OPEN | 1/1 | CONTAIN+ESC | 1/1 |
| 330 | 1 | 0.850 | 0.000 | — | 0.000 | ATTACK | ATTACK | 0.755 | 1 | frontend | LOW | OPEN | 1/1 | CONTAIN+ESC | 1/1 |
| 348..478 | 66 | 0.850 | 0.978 | — | 0.000 | COMBINATION | ATTACK+OPERATIONAL_FAULT | 0.755 | 2 | frontend+payment | LOW | OPEN | 2/2 | ESCALATE+CONTAIN+ESC | 2/2 |
| 480..514 | 18 | 0.850 | 0.992 | — | 0.000 | COMBINATION | ATTACK+OPERATIONAL_FAULT | 0.755 | 2 | frontend+payment | LOW | OPEN | 2/2 | ESCALATE+CONTAIN+ESC | 2/2 |
| 540 | 1 | 0.000 | 0.650 | — | 0.000 | OPERATIONAL_FAULT | NO_EVIDENCE+OPERATIONAL_FAULT | 0.598 | 2 | frontend+payment | LOW | MONITORING+OPEN | 2/2 | ESCALATE | 2/2 |
| 610 | 1 | 0.000 | 0.879 | — | 0.000 | OPERATIONAL_FAULT | NO_EVIDENCE+OPERATIONAL_FAULT | 0.598 | 3 | email+frontend+payment | LOW | MONITORING+OPEN | 3/3 | ESCALATE | 3/3 |
| 620..630 | 2 | 0.000 | 0.904 | — | 0.000 | OPERATIONAL_FAULT | NO_EVIDENCE+OPERATIONAL_FAULT | 0.609 | 3 | email+frontend+payment | LOW | MONITORING+OPEN | 3/3 | ESCALATE | 3/3 |
| 640..650 | 2 | 0.000 | 0.930 | — | 0.000 | OPERATIONAL_FAULT | NO_EVIDENCE+OPERATIONAL_FAULT | 0.620 | 3 | email+frontend+payment | LOW | MONITORING+OPEN | 3/3 | ESCALATE | 3/3 |
| 668..688 | 11 | 0.000 | 0.974 | — | 0.711 | OPERATIONAL_FAULT | NO_EVIDENCE+OPERATIONAL_FAULT | 0.620 | 4 | email+frontend+payment | HIGH | MONITORING+OPEN | 4/4 | ESCALATE | 4/4 |
| 690..718 | 15 | 0.000 | 0.868 | — | 0.711 | OPERATIONAL_FAULT | NO_EVIDENCE+OPERATIONAL_FAULT | 0.598 | 4 | email+frontend+payment | HIGH | MONITORING+OPEN | 4/4 | ESCALATE | 4/4 |
| 720..722 | 2 | 0.000 | 0.622 | — | 0.711 | OPERATIONAL_FAULT | NO_EVIDENCE+OPERATIONAL_FAULT | 0.593 | 4 | email+frontend+payment | HIGH | MONITORING+OPEN | 4/4 | ESCALATE | 4/4 |
| 728 | 1 | 0.000 | 0.000 | — | 0.000 | NO_EVIDENCE | NO_EVIDENCE | — | 4 | email+frontend+payment | LOW | MONITORING | 4/4 | ESCALATE | 4/4 |
| 904 | 1 | 0.000 | 0.380 | — | 0.431 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.586 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 906 | 1 | 0.000 | 0.385 | — | 0.436 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.585 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 908 | 1 | 0.000 | 0.390 | — | 0.442 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.583 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 910 | 1 | 0.000 | 0.395 | — | 0.448 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.582 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 912 | 1 | 0.000 | 0.400 | — | 0.453 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.580 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 914 | 1 | 0.000 | 0.405 | — | 0.459 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.579 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 916 | 1 | 0.000 | 0.410 | — | 0.465 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.577 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 918 | 1 | 0.000 | 0.415 | — | 0.470 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.576 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 920 | 1 | 0.000 | 0.420 | — | 0.476 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.574 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 922 | 1 | 0.000 | 0.425 | — | 0.482 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.573 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 924 | 1 | 0.000 | 0.430 | — | 0.487 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.571 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 926 | 1 | 0.000 | 0.435 | — | 0.493 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.570 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 928 | 1 | 0.000 | 0.440 | — | 0.499 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.568 | 5 | email+frontend+payment | MEDIUM | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 930 | 1 | 0.000 | 0.445 | — | 0.504 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.567 | 5 | email+frontend+payment | HIGH | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 932 | 1 | 0.000 | 0.450 | — | 0.510 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.565 | 5 | email+frontend+payment | HIGH | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 934 | 1 | 0.000 | 0.455 | — | 0.516 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.564 | 5 | email+frontend+payment | HIGH | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 936 | 1 | 0.000 | 0.460 | — | 0.521 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.562 | 5 | email+frontend+payment | HIGH | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 938 | 1 | 0.000 | 0.465 | — | 0.527 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.560 | 5 | email+frontend+payment | HIGH | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 940 | 1 | 0.000 | 0.470 | — | 0.533 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.559 | 5 | email+frontend+payment | HIGH | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 942 | 1 | 0.000 | 0.475 | — | 0.538 | EXPECTED_EVENT | EXPECTED_EVENT+NO_EVIDENCE | 0.557 | 5 | email+frontend+payment | HIGH | MONITORING+OPEN+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 946 | 1 | 0.000 | 0.000 | — | 0.000 | NO_EVIDENCE | NO_EVIDENCE | — | 5 | email+frontend+payment | LOW | MONITORING+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 3/5 |
| 1004 | 1 | 0.000 | 0.000 | — | 0.000 | NO_EVIDENCE | NO_EVIDENCE | — | 5 | email+frontend+payment | LOW | MONITORING+RESOLVED | 5/5 | SUPPRESS+ALERT+ESCALATE | 2/5 |

Final verification detail:

- Incident over `frontend`:
  - `temporal_causality`: **PASSED** — frontend was observed misbehaving first, no later than any of the 2 episodes it is credited with
  - `trace_coverage`: **PASSED** — all 1 named services were covered by telemetry
  - `dependency_validity`: **PASSED** — every named service and dependency edge exists in the committed topology
  - `memory_similarity`: **BOOTSTRAP** — incident memory holds 0 of the 20 entries this check needs; passed vacuously as a bootstrap, not as a confirmation
- Incident over `checkout, frontend, payment`:
  - `temporal_causality`: **PASSED** — payment was observed misbehaving first, no later than any of the 3 episodes it is credited with
  - `trace_coverage`: **PASSED** — all 3 named services were covered by telemetry
  - `dependency_validity`: **PASSED** — every named service and dependency edge exists in the committed topology
  - `memory_similarity`: **BOOTSTRAP** — incident memory holds 0 of the 20 entries this check needs; passed vacuously as a bootstrap, not as a confirmation
- Incident over `email`:
  - `temporal_causality`: **PASSED** — email was observed misbehaving first, no later than any of the 1 episodes it is credited with
  - `trace_coverage`: **PASSED** — all 1 named services were covered by telemetry
  - `dependency_validity`: **PASSED** — every named service and dependency edge exists in the committed topology
  - `memory_similarity`: **BOOTSTRAP** — incident memory holds 0 of the 20 entries this check needs; passed vacuously as a bootstrap, not as a confirmation
- Incident over `frontend`:
  - `temporal_causality`: **PASSED** — frontend was observed misbehaving first, no later than any of the 1 episodes it is credited with
  - `trace_coverage`: **PASSED** — all 1 named services were covered by telemetry
  - `dependency_validity`: **PASSED** — every named service and dependency edge exists in the committed topology
  - `memory_similarity`: **BOOTSTRAP** — incident memory holds 0 of the 20 entries this check needs; passed vacuously as a bootstrap, not as a confirmation
- Incident over `frontend`:
  - `temporal_causality`: **PASSED** — frontend was observed misbehaving first, no later than any of the 1 episodes it is credited with
  - `trace_coverage`: **PASSED** — all 1 named services were covered by telemetry
  - `dependency_validity`: **PASSED** — every named service and dependency edge exists in the committed topology
  - `memory_similarity`: **BOOTSTRAP** — incident memory holds 0 of the 20 entries this check needs; passed vacuously as a bootstrap, not as a confirmation

Final decision detail:

- Incident over `frontend`: **SUPPRESS** (`resolved-incident-needs-nothing`, target `—`, evidence at +1004s)
  - every symptom of this incident closed and stayed closed for the configured quiet period, so there is nothing left to act on
- Incident over `checkout, frontend, payment`: **ESCALATE_TO_HUMAN** (`hand-over-a-fault-we-cannot-confirm`, target `—`, evidence at +480s)
  - services are degrading but the hypothesis is unconfirmed or too weak to act on, so a person decides
  - approval: the ladder routes this to a person: services are degrading but the hypothesis is unconfirmed or too weak to act on, so a person decides
  - approval: 3 services are symptomatic, which is a storm rather than a single fault
- Incident over `email`: **SUPPRESS** (`resolved-incident-needs-nothing`, target `—`, evidence at +1004s)
  - every symptom of this incident closed and stayed closed for the configured quiet period, so there is nothing left to act on
- Incident over `frontend`: **ESCALATE_TO_HUMAN** (`hand-over-a-fault-we-cannot-confirm`, target `—`, evidence at +668s)
  - services are degrading but the hypothesis is unconfirmed or too weak to act on, so a person decides
  - approval: the ladder routes this to a person: services are degrading but the hypothesis is unconfirmed or too weak to act on, so a person decides
- Incident over `frontend`: **ALERT** (`context-explains-the-surge`, target `—`, evidence at +904s)
  - context-explains-the-surge: the surge deforms no behavioral ratio and degrades no service, so the context that explains its volume explains all of it; raised by a-live-incident-that-matters-is-never-silent
