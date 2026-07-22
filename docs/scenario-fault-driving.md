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

Stimuli use offsets from the scenario's measured k6 anchor, not process start
or wall-clock guesses. Adjacent intervals may share a target, but overlapping
intervals for the same flag or experiment fail validation. Rates remain capped
by the existing k6 contract, and the testbed NetworkPolicies remain the outer
blast-radius boundary.

## Capture evidence

Every new raw-bus capture retains two separate public facts:

1. `public/schedule.json` contains the declared stimulus plan.
2. `public/enrichment/stimulus-executions.json` contains the measured UTC
   start/end timestamps after injection and cleanup completed.

The private answer key contains `symptom_intervals` with kind, service, signal
and half-open offsets. Existing Phase 1 captures have neither field and remain
valid: both captured schedules and scorer-only label models default the new
collections to empty.
