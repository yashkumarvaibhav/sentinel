# ADR-0001 — Ingest topology: the collector fans out, the processors consume the bus

- **Status:** accepted
- **Date:** 2026-07-20
- **Context:** Phase 0.7, before any detection code consumes telemetry

## Context

Telemetry arrives as OTLP from the instrumented testbed. Two things need it, and
they need it differently:

- **Stores** (VictoriaMetrics, Loki, Tempo) need it for querying, dashboards,
  enrichment and human investigation.
- **The decision path** (ingest normalizer → detection → decision → action)
  needs it as an ordered, replayable stream, because the entire evaluation
  discipline rests on being able to re-run a recorded incident and get the same
  decisions out.

If the decision path read from the stores, replay determinism would depend on
store internals: retention windows, compaction timing, query-time downsampling,
late-arriving data quietly changing an already-answered query. None of that is
reproducible, and all of it is invisible in a diff.

## Decision

**The OTel Collector fans out to the stores *and* publishes raw OTLP onto the
bus. Processors consume the bus only.**

```
testbed ──OTLP──> collector ──┬──> VictoriaMetrics / Loki / Tempo   (sinks, enrichment, humans)
                              └──> Redpanda: otlp.raw.metrics / .logs / .traces      (raw)
                                        │
                                        └──> ingest normalizer ──> obs.normalized ──> detection …
```

Four consequences, all deliberate:

1. **The collector owns no domain mapping.** It publishes OTLP as it received
   it. The normalizer (slice 1.5) is the single owner of OTLP → `Observation`.
2. **The capture boundary is the raw topics.** A capture records what arrived,
   before interpretation — so replaying a capture re-runs normalization, and a
   normalizer bug is reproducible from an old capture rather than baked into it.
3. **Stores are sinks, not sources, for scored decisions.** Anything a verdict,
   RCA or golden depends on comes from the bus or from a capture-scoped
   snapshot. Live-store reads are display-only and never a scored input.
4. **Processors never read the stores in the decision path.** Enrichment that
   genuinely needs a store gets a snapshot recorded alongside the capture.

## Delivery semantics

Replay is only bit-exact if the bus is deterministic in the ways that matter:

- **At-least-once delivery** with **deterministic event IDs** — `sha256` over
  the canonical normalized evidence (service, signal, event-time, value, unit,
  attributes and evidence references). The same OTLP point keeps the same ID
  even if its envelope is rebatched or JSON object keys arrive in another order.
- **Dedup on event ID** at the consumer boundary, before any stateful window
  update. Duplicates are counted into a meta-metric; silent dedup that nobody
  can see is how a broken producer stays hidden.
- **Partition keys** are `(service, signal)`. Ordering is guaranteed per key,
  which is the only ordering detection actually depends on; cross-service
  ordering is resolved by event time and the watermark, never by arrival order.
- **Event time, not arrival time.** Windows key on source timestamps with a
  bounded-lateness watermark per stream. Data later than the watermark is
  counted into a meta-metric and dropped — never merged into a closed window,
  because that would silently change an answer already given.
- **A dead-letter topic** per pipeline stage. A message that cannot be parsed
  or normalized goes to the DLQ with its reason; it is never dropped quietly and
  never retried forever.
- **Offsets are the capture boundary.** A capture is `(topic, partition,
  start-offset, end-offset)` per stream plus the snapshot of any enrichment it
  read. Replay re-consumes exactly that range.

## Alternatives considered

- **Processors read the stores directly.** Simpler to build, and fatal to the
  evaluation discipline: replay determinism would depend on retention,
  compaction and query semantics we do not control.
- **The collector normalizes into `Observation` and publishes that.** Fewer
  moving parts, but it puts domain logic in a config-only component and makes
  captures un-reprocessable — an old capture could never exercise a fixed
  normalizer.
- **Bus only, no stores.** Loses the query surface humans need during an
  incident, and loses trace-level RCA entirely.

## Consequences

- Redpanda is on the critical path for detection; its health is part of
  `/api/health` and its lag is a meta-monitored signal.
- The raw topics carry duplicate data (also in the stores). Accepted, and
  bounded by retention on both sides.
- `make score CAPTURE=1` becomes possible: a recorded offset range replayed
  through the same code must produce byte-identical decisions.
