# Change-point and saturation detection

Phase 2.4 detects traffic-independent resource exhaustion from raw resource measurements. The
detector accepts only event-time evidence for one service, signal, and fixed capacity. Traffic,
event context, and scenario labels are not inputs, so a legitimate request-rate surge cannot by
itself produce a resource symptom.

## Propose, then verify

PELT (`ruptures`, L2 cost) proposes structural breakpoints over resource utilization. A proposal
becomes a `SATURATION` symptom only when deterministic checks confirm all of the following:

1. the window and both PELT segments meet configured sample minima;
2. the post-change segment is monotonically increasing for the configured fraction of steps;
3. its event-time utilization slope clears the configured minimum;
4. its average level is above the pre-change level; and
5. remaining resource headroom is at or below the configured maximum.

The emitted evidence records the change index and timestamp, before/after levels, raw and
utilization slopes, increasing fraction, headroom, headroom ratio, and every sorted raw evidence
reference. Symptom identity is a canonical content hash, so replay and caller-order changes cannot
alter it. The score starts at `1 - maximum_headroom_ratio` and rises linearly to `1.0` at
`full_score_headroom_ratio`.

Short and flat series return an explicit evaluation with no proposed change and no symptom.
Malformed, mixed-identity, non-finite, over-capacity, duplicate-ID, or duplicate-time evidence is
rejected before PELT, so invalid input can never alert. Thresholds and the PELT penalty live under
`change_point_saturation` in `config/detector-params.yml`; no detector threshold is embedded in
runtime logic.
