# Log template burst detection

Sentinel mines raw, label-free log lines into service-local Drain3 templates. Each fixed event-time
window becomes a set of template frequencies, measured in messages per second, and those rates are
compared with context-blind learned baselines. A trusted event does not excuse a new or suddenly
dominant error template.

The miner sorts every window by `(timestamp, log_id)` before updating state, uses no persistence or
wall-clock value in an output, and keeps one Drain3 model per service. Template identity is the
content hash of the service plus Drain3's replay-stable cluster ID, so later wildcard
generalization does not orphan an existing baseline. Every message in the window is rematched after
training so early literal examples are counted under the final generalized template.

`config/detector-params.yml` owns Drain similarity/depth/capacity, numeric-token behavior, minimum
template count, the baseline rate floor, deformation trigger and full-score point. Missing template
baselines are treated as zero only after the minimum count is met; a single rare line therefore
cannot become a burst. Invalid timestamps, duplicate log IDs, non-finite baselines and invalid
window durations fail closed.

An emitted `LOG_BURST` names the stable template ID and mined template, current and baseline rates,
window count, relative deformation and configured trigger. Its onset is the first matching log in
event time and its evidence references contain every matching raw log ID.

## Window materialization and episode routing

`LogDetectionRunner` consumes normalized `log.record` observations in fixed,
caller-supplied event-time windows. `service_mappings` explicitly translates
collector service names to logical topology services, and startup rejects an
unknown logical target. Each service owns an independent Drain3 miner and
context-blind baseline, so traffic from one service cannot train or trigger
another service's templates.

Only records with unit `record`, value `1`, a non-empty string body and exactly
one log reference are valid evidence. A sufficient warmup window contributes
its measured template rates to the frozen baseline. After warmup, every
complete window produces one auditable status per configured logical service:

- `WARMING` while the configured sufficient baseline windows are incomplete;
- `INSUFFICIENT` for empty, sparse, malformed, duplicate or contradictory
  evidence;
- `BREACH` when at least one deterministic template deformation is verified;
  or
- `CLEAR` only after a sufficient, unambiguous window contains no verified
  burst.

Only `BREACH` and `CLEAR` advance the shared episode pipeline. Empty, partial or
ambiguous windows therefore cannot fabricate a zero, clear an alert, or mutate
the baseline. When several templates breach together, the runner routes the
largest unsaturated relative deformation into the service-level episode and
retains every detected template in the audit result.

Ticks must be exactly one configured window apart. Exact re-delivery is
idempotent even if attribute insertion order differs; conflicting same-time
input, skipped or out-of-order ticks, future evidence and reused observation
IDs with changed evidence fail closed. Raw-capture replay uses the same public
normalized observations and emits canonical transcript bytes. Runtime capture
loading never exposes private labels to this path.
