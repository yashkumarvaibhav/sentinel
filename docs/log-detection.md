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
