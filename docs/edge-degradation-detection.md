# Dependency-edge degradation detection

Phase 2.6 turns caller-to-downstream trace evidence into one deterministic
`EDGE_DEGRADED` symptom without blaming every service that happens to be active.

## Evidence contract

`EdgeDegradationDetector.evaluate` accepts one tuple of label-free dependency calls. Each call
names its caller, downstream, UTC event time, latency, failure status and replay-stable evidence
reference. A window must contain exactly one edge and unique evidence IDs. Caller order is ignored:
calls are sorted by `(timestamp, evidence_id)` before statistics or identity are computed.

The detector derives nearest-rank p95 latency and error rate from the calls. It compares those
statistics with caller-supplied, context-blind latency and error baselines; runtime labels and event
context are absent from the API. Both baselines must exist and the configured minimum sample count
must be met. Missing baselines, short windows and unconfigured edges remain explicitly insufficient
and cannot alert.

## Deterministic verification

For each dimension, relative rise is:

```text
max(current - baseline, 0) / max(baseline, configured_floor)
```

Latency or error rise may independently verify degradation at its inclusive configured trigger.
The score is the larger dimension's progress toward its configured full-score point, bounded to
`[0, 1]`. Onset is the first event-time call that crosses the verified latency boundary or the first
failed call when error degradation is verified. The symptom service is the caller, its signal is
`dependency.<downstream>`, and the note preserves both measured values, baselines, deformations,
triggers, sample count and the exact caller→downstream edge.

Every monitored edge is an explicit rule under `edge_degradation` in
`config/detector-params.yml`. Startup cross-validates each caller→downstream pair against the direct
dependencies in `config/topology.yml`; two known services do not become an edge merely because both
exist. Floors, minimum evidence, triggers and full-score points are configuration data and are part
of the runtime fingerprint.

## Window materialization and episode routing

Phase 2.8e adds `EdgeDetectionRunner` between normalized observations and the
detector. The runner accepts exact caller-supplied event-time ticks; it never
reads wall time. Each configured `(caller, rpc_service)` mapping selects only
client gRPC `span.duration_ms` observations with one trace reference. Malformed,
negative, missing-status or contradictory evidence is retained as ambiguous and
makes the affected rolling window `INSUFFICIENT`.

The first configured number of valid calls per edge forms an immutable
context-blind bootstrap baseline. Later calls enter a fixed rolling window with
a configured advance. Both policies are operator data under `edge_degradation`,
as is the bounded observation-ID deduplication capacity. Exact re-delivery of a
tick is idempotent; conflicting same-time input, out-of-order ticks, timestamp
leakage and reused IDs with different evidence fail closed.

Every advance yields one auditable status for every configured edge:

- `WARMING` while the baseline is incomplete;
- `INSUFFICIENT` when the window is sparse or contains ambiguous evidence;
- `BREACH` when the deterministic detector emits a verified symptom; or
- `CLEAR` only when a sufficient, unambiguous evaluation emits no symptom.

Only `BREACH` and `CLEAR` advance the shared episode pipeline. In particular,
missing traffic cannot fabricate a clear tick or close an active episode. Raw
capture replay uses the same normalized observations and event-time advances,
and emits canonical bytes containing the evidence statistics and lifecycle
transitions for deterministic regression checks.
