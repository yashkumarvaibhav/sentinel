# Phase 6 reliability KPI proof

- **Gate: PASS**
- Telemetry is **REAL**. Quiet-day schedules and the contained lab action are controlled testbed stimuli; no production fixture was written.
- Autonomous MTTR: **16.767s** across 1 contained real-testbed action.
- Quiet-day false acts: **0** across 2 sealed held-out captures.
- Protected-cohort integrity remains insufficient because no production protected-cohort telemetry reader is attached.

## Sources

- MTTR plan: `contained-payment-scale-1785662069`; window `2026-08-02T09:14:29.731769+00:00` → `2026-08-02T09:14:46.498303+00:00`.
- Quiet captures: `phase1-quiet-7901-v2`, `phase1-quiet-7919-v2`.
- Action config fingerprint: `df372b8418bee9cd5ba45f4e2a69c14b43bd37ce21ecf5992192cb5843b4da33`.
- Replica change: 1 → 2 → 1.

## SLO settlement

```json
{
  "after": [
    {
      "availability": 1.0,
      "availability_target": 0.999,
      "latency_p95_ms": 7.8999999999999995,
      "latency_p95_target_ms": 500.0,
      "sampled_at": "2026-08-02T09:14:46.498303Z",
      "service": "frontend",
      "status": "MEASURED"
    },
    {
      "availability": 1.0,
      "availability_target": 0.995,
      "latency_p95_ms": 87.49999999999974,
      "latency_p95_target_ms": 1000.0,
      "sampled_at": "2026-08-02T09:14:46.498303Z",
      "service": "checkout",
      "status": "MEASURED"
    }
  ],
  "before": [
    {
      "availability": 1.0,
      "availability_target": 0.999,
      "latency_p95_ms": 7.8999999999999995,
      "latency_p95_target_ms": 500.0,
      "sampled_at": "2026-08-02T09:14:31.250440Z",
      "service": "frontend",
      "status": "MEASURED"
    },
    {
      "availability": 1.0,
      "availability_target": 0.995,
      "latency_p95_ms": 87.49999999999974,
      "latency_p95_target_ms": 1000.0,
      "sampled_at": "2026-08-02T09:14:31.250440Z",
      "service": "checkout",
      "status": "MEASURED"
    }
  ]
}
```
