# Drop and telemetry-silence detection

Phase 2.5 keeps low volume and missing data as separate evidence paths. Both are deterministic,
label-free, and event-time driven.

## Measured low volume (`DROP`)

`LivenessDetector.evaluate_drop` compares one measured window with a caller-supplied expected
level. The expected level is context-aware pipeline evidence, such as the decomposition frame's
base plus event component; it is not a scenario label. The detector emits only when:

1. the exact `service.signal` has a configured rule;
2. the expected value exists and clears that stream's absolute sufficiency floor; and
3. `(expected - observed) / expected` clears the configured relative-drop trigger.

This means an expected match-night lift that fails to arrive is visible, while a legitimately
quiet stream below its absolute floor is insufficient rather than a false outage. A missing
window is never converted to `observed=0`; it belongs to the silence path.

## Missing telemetry (`SILENCE`)

`LivenessDetector.evaluate_silence` receives an explicit event-time watermark, the time the
emitter became expected, and an optional last-seen timestamp. It performs no wall-clock read.
Staleness starts at the later of registration and last evidence, so a newly registered emitter
gets the configured startup grace. A stale or never-seen configured stream emits at the exact
event-time boundary `reference + maximum_age_seconds`.

Future timestamps, local/non-UTC time, non-finite values, duplicate evidence references and
unknown configuration services fail before emission. Both symptom kinds retain sorted raw
evidence references, measured inputs and canonical content-hash identity. Per-stream absolute,
relative and age policies live under `liveness` in `config/detector-params.yml`.
