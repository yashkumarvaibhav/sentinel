# Scenario fault driving

Phase 2 scenarios can declare bounded fault stimuli alongside the existing k6
load schedule. The compiled schedule is public and contains what the lab will
do. Expected symptom intervals remain only in `private/labels.json`; detection
code never reads them.

## Supported stimuli

- `flagd` selects a flag and variant from the deployed Astronomy Shop
  `flagd-config`. The runner cannot select another ConfigMap, namespace or
  deployment. It snapshots the complete original flag document, validates all
  requested variants before the first mutation, patches the fixed ConfigMap,
  and rolls only `deployment/flagd`. The exact original document is restored
  after the interval and again during error cleanup if necessary.
- `chaos_mesh` selects a committed manifest under `lab/testbed/chaos/`. The
  manifest must have the requested name, use an allowlisted Chaos Mesh kind,
  select only the `otel-demo` namespace, contain no external target, and carry
  a self-expiring duration of at most five minutes. The runner deletes the
  experiment at the requested end offset and during error cleanup.
- `k6_journey` runs the fixed `checkout` path at no more than 5 iterations/s.
  The target remains the in-cluster `frontend-proxy`; the DSL cannot override
  it. Each iteration gets a product, adds it to a cart, then places the order.
  A distinct `sentinel-stimulus/` user agent makes its real ingress spans
  measurable without treating the requested rate as evidence. The runner
  accepts the journey only after at least 95% of the expected three ingress
  spans per iteration arrive, and always deletes its owned Job and ConfigMap.

Stimuli use offsets from the scenario's measured k6 anchor, not process start
or wall-clock guesses. The live runner waits for the complete ten-span marker
burst, or for a partial burst to remain stable across three polls if telemetry
lost a marker, and selects the same final timestamp that replay selects.
Adjacent intervals may share a target, but overlapping intervals for the same
flag, experiment or journey fail validation. Rates remain capped by the
existing k6 contract, and the testbed NetworkPolicies remain the outer
blast-radius boundary.

## Capture evidence

Every new raw-bus capture retains two separate public facts:

1. `public/schedule.json` contains the declared stimulus plan.
2. `public/enrichment/stimulus-executions.json` contains the measured UTC
   start/end timestamps after injection and cleanup completed.

The private answer key contains `symptom_intervals` with kind, service, signal
and half-open offsets. A label may reference a stimulus ID; capture then
materializes its offsets from that stimulus's measured start/end timestamps
relative to the measured anchor. The requested interval is never substituted
for execution evidence. Existing Phase 1 captures have neither field and remain
valid: both captured schedules and scorer-only label models default the new
collections to empty.
