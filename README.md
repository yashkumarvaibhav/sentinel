# Sentinel

**Context-aware autonomous observability.** Sentinel separates a traffic surge into the
part the world *explains* — a scheduled event, a launch, a match night — from the part it
*cannot*: an attack or a self-inflicted fault hiding inside the surge. It root-causes the
real incident and takes graded, reversible, human-gated action, with stated evidence for
every decision.

## The idea

> **An event explains volume, not behavior.**

A legitimate surge multiplies request *rates* but preserves behavioral *ratios* — IP
entropy, auth-failure rate, SYN:ACK balance, path and conversion mix. Attacks deform those
ratios. Internal faults deform latency and saturation instead. So Sentinel builds a
context-aware expected band, watches the behavioral ratios independently, and treats
whatever the band cannot explain as guilty until proven innocent — even while the
legitimate flood roars on.

Two patterns follow from that, and they are structural:

1. **Decompose, don't threshold.** Every surge is split into
   `observed = explained_base + explained_event + unexplained_residual`.
   The residual is the product.
2. **Propose, then verify.** Any reasoning step only *proposes*. A deterministic,
   non-model verifier and a policy gate must confirm the proposal against telemetry
   before anything is acted on. Safety-critical fields — human-approval flags, code
   localization, action targets — are computed from evidence, never taken on trust.

## Repository layout

```
platform/     Python services — the seven planes
  contracts/    Pydantic models → JSON Schema → TypeScript types (one source of truth)
  context/      calendars, connectors, trust scoring, expected bands
  ingest/       bus consumers, normalization, feature extraction
  detection/    envelopes, ratios, log templates, change points, drops, episodes
  decision/     evidence agents, fusion verdict, causal collapse, verifier, policy gate
  rca/          trace critical path, change correlation, code localization, postmortem
  action/       actuators (plan/simulate/apply/verify/revert), ladders, guards, breaker
  audit/        hash-chained decision ledger
  ml/           training pipelines, registry, calibration, drift
  api/          FastAPI gateway — REST + SSE
  common/       config loading, storage clients, seeded clock
web/          React + TypeScript + Vite command center
lab/          evaluation lab — testbed, scenarios, load, attacks, scoring harness
config/       topology, event calendar, SLOs, cohorts, detector params, Rego policies
deploy/       docker compose, Caddyfile, service units, dashboard provisioning
docs/         ADRs, runbooks, port map, resource budget
```

## Running it

```bash
make up        # bring the stack up (docker compose, isolated network)
make verify    # lint, typecheck and test everything
make lab-up    # bring up the instrumented testbed that generates real telemetry
make score     # run the scoring harness on held-out seeds
make down      # tear the stack down
```

Host ports are confined to the **8040–8049** block and bound to `127.0.0.1`; see
`docs/ports.md`. Configuration is data — YAML and Rego under `config/` — not constants
buried in engine code. Secrets come from the environment only; copy `.env.example` to
`.env` to set them.

## Ground rules

- **Real telemetry.** Signals come from a real instrumented service mesh under real load
  and real fault injection, not from scripted numbers.
- **Honesty labels.** Anything simulated is labeled `SIMULATED` in the UI and in reports;
  measured things are labeled `REAL`. Scripted evidence is never presented as measured.
- **The scoreboard is the definition of done.** Detection, decision and action logic are
  written against the scoring harness, which runs on held-out seeds never used during
  development. Ground-truth labels never reach the runtime — enforced by a runtime guard
  and by tests over the import graph.

## Status

Early build. The foundation phase — repository, stack, testbed, CI — is in progress; the
decomposition core, decision plane, action plane and command center follow.

## License

Not yet licensed for redistribution.
