# Phase 2 resource-input capability proof

## Result

**QUALIFIED — REAL telemetry / SIMULATED workload-context-fault.** The accepted development
capture `phase2-cascade-401-dev-v7` contains a topology-valid container working-set series and a
constant Kubernetes hard memory limit for every configured logical service. This proves the input
contract needed to materialize `ResourceSample` windows; it does **not** claim that this no-leak
capture contains a `SATURATION` incident.

The audit consumes only normalized public `Observation` values. It does not open the capture's
private labels, infer capacity from virtual memory, or treat dynamic language-runtime heap sizes as
hard capacity.

## Capture provenance

| Field | Value |
|---|---|
| Capture | `phase2-cascade-401-dev-v7` |
| Scenario / seed | `cascade_night` / `401` (`development`) |
| Raw records / bytes | 233 / 43,447,180 |
| Scenario bounds | `2026-07-22T15:41:34.422588Z` → `2026-07-22T15:43:58.422588Z` |
| Capture config fingerprint | `645eada691f3098f73352d67040ac78e1f48f943f768209696ee558e531e1869` |
| Raw replay SHA-256 | `4c234f6e159dbf0abe7153364cc983e202c6fddac637a184a7b79c954bd7ae26` |
| Capability transcript SHA-256 | `18b78aadba560586849e6efd1796455a24af89e4920fb1eb8ef2f9a8cedba068` |
| Raw dead letters | 0 |
| Private labels read | **false** |

## Qualified streams

Each row uses `container.memory.working_set` as measured usage and
`k8s.container.memory_limit` as fixed capacity. Identity is scoped by namespace, container name,
and pod UID. All points are byte-valued gauges; all capacities are positive and constant; no used
value exceeds its capacity.

| Service | Used points | Capacity observations | Fixed capacity | Evidence interval |
|---|---:|---:|---:|---|
| cart | 14 | 15 | 160 MiB | `15:41:40.958098Z` → `15:43:50.952566Z` |
| checkout | 14 | 15 | 20 MiB | `15:41:40.958098Z` → `15:43:50.952566Z` |
| frontend | 14 | 15 | 250 MiB | `15:41:40.958098Z` → `15:43:50.952566Z` |
| payment | 14 | 15 | 140 MiB | `15:41:40.958098Z` → `15:43:50.952566Z` |

The prior accepted v6 capture remains an explicit negative oracle: all four topology streams are
`INSUFFICIENT/MISSING_USED_AND_CAPACITY`. Its kubelet receiver addressed the k3d node's Docker IP
from the pod network and could not reach port 10250. The v7 collector DaemonSet uses the contained
k3d node network and its local kubelet loopback; this publishes no physical-host port.

## Rejections and guardrails

The capability audit fails closed for missing usage or capacity, missing pod identity, unsupported
unit/kind, short series, changing or non-positive capacity, negative usage, usage above capacity,
and conflicting values at one timestamp. Non-topology containers and dynamic V8 heap limits are
ignored rather than silently reinterpreted. Input order and topology order produce byte-identical
audit transcripts.

Two rejected v7 attempts wrote no capture artifact. Both exposed insufficient k6 worker
preallocation for successful checkout calls: the requested 2 rps could require more than two
concurrent iterations, causing dropped arrivals. Checkout jobs now preallocate bounded concurrency
without changing the rate, duration, target, fault, or the zero-dropped-iterations threshold.
