# Sentinel

**A Python observability lab for decomposing traffic surges, distinguishing behavioral anomalies
from operational faults, and verifying reversible remediation against evidence.**

[Open the live command center](https://sentinel.yashkumarvaibhav.me)

Sentinel starts from one testable idea:

> **An event explains volume, not behavior.**

A launch, sale, or match can multiply request rates while preserving relationships such as source-IP
entropy, authentication-failure rate, SYN:ACK balance, path mix, and conversion. An attack deforms
those ratios; an internal fault deforms latency, saturation, or dependency behavior. Sentinel models
the expected event load, isolates the unexplained residual, and requires a deterministic verifier to
confirm a proposed diagnosis before any action can run.

The lab uses **real telemetry from an instrumented service mesh under load**. Faults and attacks are
injected and therefore **simulated**; they are not production incidents or real-world attacks.

## Two design patterns

1. **Decompose, do not threshold.** Each surge is represented as
   `observed = baseline + event-explained + unexplained residual`. The residual, not the whole crowd,
   is the anomaly signal.
2. **Propose, then verify.** Statistical and causal components may propose an explanation. A
   deterministic four-check verifier re-derives it from telemetry and committed topology before an
   idempotent, leased, reversible action is eligible.

Safety-critical fields such as action targets and human-approval requirements are computed from
evidence rather than accepted from a model output.

## Architecture

```text
OpenTelemetry demo workloads under k6 load and injected faults
  └─ OpenTelemetry Collector
       └─ Redpanda / Kafka topics
            └─ typed normalization + DLQ + bounded deduplication
                 ├─ ClickHouse telemetry
                 └─ PostgreSQL incidents, decisions, actions, audit records

Detection
  ├─ event-aware decomposition and behavioral ratios
  ├─ change points, log templates, drops, and episode lifecycle
  ├─ LightGBM quantile envelopes + context-blind twin
  ├─ LSTM sequence autoencoder
  └─ split-conformal calibration + streaming drift monitors

Decision
  ├─ independent evidence axes
  ├─ topology-aware causal collapse
  ├─ deterministic verifier
  └─ YAML policy gate

Action
  ├─ Kubernetes, Envoy, and feature-flag actuators
  ├─ plan → simulate → apply → verify → revert
  └─ leases, idempotency keys, circuit breaker, and hash-chained audit

FastAPI REST/SSE gateway → React + TypeScript command center
```

## Technology

| Layer | Stack |
| --- | --- |
| Backend | Python 3.12, FastAPI, Pydantic v2, strict mypy |
| Streaming and storage | Redpanda/Kafka, ClickHouse, PostgreSQL, OpenTelemetry |
| Detection and ML | ruptures, drain3, LightGBM, PyTorch, statsmodels, scikit-learn, river |
| Testbed | k3d/k3s, OpenTelemetry Demo, Chaos Mesh, k6, Envoy, flagd |
| Frontend | React 19, TypeScript, Vite, Tailwind, ECharts |
| Verification | pytest, Hypothesis, Vitest, Playwright, ruff, mypy, four-job GitHub Actions CI |

Pydantic contracts generate JSON Schema and TypeScript types, keeping the Python and browser payloads
on one checked interface.

## What is measured

Every committed report carries telemetry and stimulus honesty labels plus the underlying source IDs.

| Result | Value | Evidence boundary |
| --- | ---: | --- |
| Minimum symptom recall | 1.000 | 7 labelled symptoms across held-out captures |
| Detection latency p95 | 179.64 s | 20 matched symptom episodes |
| Autonomous MTTR | 16.77 s | 1 contained real-testbed action |
| Quiet-day false actions | 0 | 2 held-out decision replays |

The harder stored-state live-run report is also committed and **fails its gate**: 0.800 decision
accuracy, 0.400 decision-plus-reason accuracy, 0 attack recall, and 0.750 origin accuracy, while
still taking 0 false actions. The repository preserves that failed result because a system that
grades only its best replay is not an honest operational evaluation.

- [`docs/reports/latest-score-proof.json`](docs/reports/latest-score-proof.json)
- [`docs/reports/latest-reliability-proof.json`](docs/reports/latest-reliability-proof.json)
- [`docs/reports/phase-6-live-run-score.md`](docs/reports/phase-6-live-run-score.md)

## Repository map

```text
platform/
  contracts/    Pydantic → JSON Schema → TypeScript contracts
  context/      calendars, connectors, trust, expected bands
  ingest/       Kafka consumers, normalization, windows, DLQ
  detection/    decomposition, ratios, change points, logs, episodes
  decision/     evidence axes, fusion, causal collapse, verifier, policy
  action/       actuators, ladders, guards, leases, rollback, journal
  audit/        hash-chained ledger and independent verifier
  ml/           envelopes, forecasts, autoencoder, calibration, drift
  api/          FastAPI REST and SSE gateway
web/            React and TypeScript command center
lab/            testbed, scenarios, load, attack/fault injection, scoring
config/         topology, events, SLOs, cohorts, detectors, YAML policy
deploy/         Docker Compose, Caddy, service units, dashboards
docs/           ADRs, runbooks, scoring methodology, measured reports
```

`platform/rca/` is currently a placeholder. Trace critical-path reconstruction, change correlation,
code localization, and postmortem generation are **not implemented** and are not presented as shipped
features. Policy evaluation is YAML-based; the repository does not contain OPA or Rego.

## Running locally

### Prerequisites

- Docker with Compose
- GNU Make
- Enough memory and disk for the 15-service stack

```bash
git clone https://github.com/yashkumarvaibhav/sentinel.git
cd sentinel
cp .env.example .env
make up
```

The front door binds to loopback in the `8040–8049` port block; see
[`docs/ports.md`](docs/ports.md). Open <http://127.0.0.1:8041> after the health checks settle.

Useful commands:

```bash
make verify       # Python and web lint, types, and tests
make lab-up       # instrumented k3s testbed
make score        # held-out scoring harness
make golden       # deterministic replay contracts
make down         # stop the application stack
```

Secrets are read from the environment. `.env.example` contains placeholders only.

## CI and evaluation hygiene

The four CI jobs check:

1. Python formatting, lint, strict typing, and tests.
2. Web typing, lint, tests, and production build.
3. Held-out scoring, golden replay, and whether committed reports match the current code.
4. A complete Compose stack, migrations, storage round trips, build-stamp identity, and the
   north-star flow through a real browser and gateway.

Ground-truth scenario labels are excluded from runtime packages by an AST import-graph test. Model
training is CPU-pinned and seeded for reproducible evaluation. `SIMULATED` and `REAL` labels travel
with evidence into reports and the command center.

## Current limits

- This is a lab-scale, single-environment system with no production traffic or user population.
- Attack and fault stimuli are injected; only the resulting telemetry path is real.
- The public gate uses a shared-secret header for protected API actions, not user authentication or
  RBAC.
- The RCA package is not implemented, and there is no OPA/Rego or eBPF path.
- The action plane is designed for safe retry and rollback, but the measured MTTR result has one
  contained-action sample and should be read at that scope.

## License

No license has been granted for redistribution.
