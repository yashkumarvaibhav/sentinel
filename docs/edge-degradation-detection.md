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
