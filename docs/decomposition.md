# Deterministic surge decomposition

The Phase 1 baseline is deliberately context-blind. Each configured
`service.signal` owns an independent warmup and EWMA state. Warmup uses the
median of the configured number of ordinary points; an observation covered by
a positive event lift cannot seed that state. After warmup, the engine updates
the EWMA only when the observed value remains within the greater of the
configured absolute noise floor and the baseline-relative anomaly gate.
Consequently, a sustained spike and a legitimate event surge both leave the
ordinary baseline unchanged.

For a baseline `b`, every active context that explicitly names the signal adds

```text
b * max(expected_total_multiplier - 1, 0) * trust_score
```

to `explained_event`. Independent overlapping contexts add; their sorted IDs
remain on the frame as evidence. A context never explains a signal it does not
name. The engine then preserves the exact accounting identity:

```text
observed = explained_base + explained_event + residual
```

The expected band is centered on `explained_base + explained_event` and its
half-width is the greater of the per-signal absolute floor and the configured
relative tolerance. An in-band residual is retained as a signed arithmetic
fact but scores zero. A residual outside the band gets a finite score in
`[0, 1]` based on its excess beyond the band.

Frame IDs are SHA-256 digests of canonical frame contents, including sorted
context IDs and caller-provided UTC event time. Retrying an observation returns
the cached frame and does not advance baseline state twice. The async worker
writes every post-warmup frame through the ClickHouse repository without
sampling; stable IDs make sink retries logically idempotent.

## Current boundary

The engine expects each stream in event-time order; the Phase 1.5 window and
watermark boundary owns late-data rejection. The EWMA is the deterministic
zero-model fallback. Phase 3 may supply learned envelopes, but must retain this
path when no model is registered.

`make verify` covers baseline contamination, context signal isolation,
overlaps, warmup, absolute floors, stable identity and retry behavior.
`make verify-storage` additionally runs the real engine-to-ClickHouse boundary
and reads back two consecutive full-resolution frames.
