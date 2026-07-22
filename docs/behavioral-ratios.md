# Behavioral ratio monitors

Sentinel treats a trusted event as an explanation for volume, never for changed behavior. The
deterministic ratio layer therefore compares dimensionless or scale-invariant window statistics
with context-blind baselines and proposes `RATIO_DEFORM` evidence when behavior changes.

## Metrics

| Metric | Window statistic | Abnormal direction |
|---|---|---|
| Source entropy | Shannon entropy in bits over source/IP request counts | lower |
| Auth failure | failures / attempts | higher |
| SYN:ACK | SYN count / ACK count | higher |
| RPC amplification | retries / requests | higher |
| HTTP path entropy | Shannon entropy in bits over path request counts | lower |
| Conversion | conversions / eligible visits | lower |
| Inter-arrival variation | population standard deviation / mean of inter-arrival gaps | lower |
| Crowd coherence | positive part of Pearson correlation across paired traffic/KPI windows | lower |

Multiplying every count in a window by the same positive factor leaves count ratios and entropy
unchanged. Inter-arrival coefficient of variation is unchanged by a positive time-unit/rate scale,
and Pearson correlation is unchanged by positive KPI rescaling. This is why a larger but
behaviorally similar fan crowd remains ordinary while concentrated, non-converting,
machine-regular or KPI-incoherent traffic remains visible.

## Deformation and evidence rules

For current value `x`, baseline `b`, configured baseline floor `f`, and abnormal direction `d`,
the monitor computes the directed positive delta and divides it by `max(b, f)`. It emits only when
that relative deformation reaches the configured trigger. The symptom score is deformation divided
by the configured full-score point, clipped to `[0, 1]`.

All floors, triggers, score-saturation points, sequence minimums and unbounded-ratio ceilings live
under `behavioral_ratios` in `config/detector-params.yml`. They are validated at startup and are
included in the runtime configuration fingerprint.

Empty `0/0` count windows, empty category windows, too-short sequences and constant Pearson inputs
are insufficient evidence: `current` and deformation stay absent and no symptom is emitted.
Impossible subset counts such as failures greater than attempts fail closed. Positive-over-zero
unbounded ratios use the configured finite ceiling and disclose that cap in the symptom note.
Every emitted symptom also names its current value, baseline, deformation, trigger, raw window
summary and sorted evidence references.

## HTTP ingress window materialization

`IngressRatioDetectionRunner` currently materializes the two ratios directly
supported by normalized HTTP server spans: path entropy and inter-arrival
variation. Explicit YAML mappings select telemetry services and their logical
topology services. Other services, non-server spans and non-millisecond signals
are ignored; event context and capture labels are absent from the API.

A valid request requires a non-negative `span.duration_ms`, server span kind,
exactly one trace link and an absolute HTTP(S) `http.url`. The path component is
the category; query parameters are deliberately excluded. A trace may contain
several distinct ingress requests, so the normalized observation ID—not the
trace ID—is the request/evidence identity. Semantic observation-ID deduplication
still makes at-least-once replay idempotent.

Each configured logical service owns independent frozen baselines for both
metrics. Window size, sufficient request count, warmup windows, dedup capacity
and telemetry-to-logical mappings are versioned under
`behavioral_ratios.ingress_windows`. Every complete event-time window yields
one status per service and metric:

- `WARMING` for a valid measurement while its baseline is incomplete;
- `INSUFFICIENT` for empty, sparse, malformed, ambiguous or degenerate
  evidence;
- `BREACH` for a deterministic deformation; or
- `CLEAR` only for a sufficient, unambiguous measurement with no deformation.

Only `BREACH` and `CLEAR` advance the independent `RATIO_DEFORM` episode keys.
Incomplete capture tails are never converted into ticks. Exact retry is
idempotent, while conflicting same-time input, gaps, reordering, future
evidence and observation-ID reuse fail closed.

Source entropy, auth failure, SYN:ACK, RPC retry amplification, conversion and
crowd coherence remain detector APIs only until their required raw semantics
exist. In particular, the materializer does not treat a cluster peer as an end
user source, infer conversion from unrelated spans, infer retries without an
explicit retry marker, or pair unrelated KPIs.
