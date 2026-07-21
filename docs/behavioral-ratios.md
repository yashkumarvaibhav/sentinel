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
