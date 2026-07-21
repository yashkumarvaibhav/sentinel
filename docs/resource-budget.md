# Resource budget

Sentinel runs on a shared host. Other people's workloads live here, so
**resource contention is a production incident of someone else's system** —
treated with the same seriousness as taking down a port they own.

Measured on 2026-07-20 (`free -g`, `df -h`, `nvidia-smi`):

| Resource | Total | Actually available |
|---|---|---|
| RAM | 125 GB | **~34 GB** (≈89 GB in use by other workloads; swap fully consumed) |
| Disk `/` | 936 GB | 809 GB (docker data root) |
| Disk `/home` | 44 TB | 11 TB |
| CPU | 128 cores | shared, uncapped |
| GPU | RTX A6000 48 GB + RTX A4000 16 GB | classical ML / time series / embeddings only |

**Re-measure before sizing anything.** Availability moves with other users'
jobs; the numbers above are a snapshot, not a guarantee.

## Profiles

Three named profiles. Every number is a hard limit expressed in the compose
file or the job spec — not an intention.

### `dev` — the default `make up` stack

| Service | Memory limit | Notes |
|---|---|---|
| postgres | 512 MB | metadata, incidents, audit chain |
| clickhouse | 2 GB | `max_server_memory_usage` capped to 1.5 GB inside the container |
| redpanda | 1.5 GB | `--memory=1G --smp=1 --overprovisioned` |
| redpanda-console | 256 MB | dev convenience only |
| victoriametrics | 1 GB | `-memory.allowedBytes=768MB` |
| loki | 512 MB | ingestion rate-capped at 8 MB/s |
| tempo | 512 MB | ingestion rate-capped at 15 MB/s |
| otel-collector | 512 MB | `memory_limiter` sheds load at 384 MiB |
| grafana | 512 MB | dashboards only |
| ingest | 384 MB | bounded 100k-ID LRU; no published port |
| gateway | 512 MB | API and dependency probes |
| web | 256 MB | Caddy front door and static build |
| **Total** | **≈8.5 GB** | ~31% of the 27 GB available when ingest landed |

### `scoring` — capture replay, no live testbed

Runs the pipeline against recorded telemetry. No k3s, no chaos, no load
generator. Budget: the `dev` stack plus **≤4 GB** for the scoring processes.
This is the profile CI-equivalent runs use, and it must fit comfortably
alongside whatever else the box is doing.

### `nightly-lab` — live testbed run

The only profile that starts k3s and the instrumented mesh. Additional caps:

| Component | Cap | Where |
|---|---|---|
| k3s server node | 4 GB | `lab/testbed/k3d-cluster.yaml` (`serversMemory`) |
| k3s agent node | 8 GB | `lab/testbed/k3d-cluster.yaml` (`agentsMemory`) |
| instrumented demo mesh | fits inside the agent's 8 GB, per-pod limits set | helm values |
| chaos experiments | stay inside pod limits — no host-level stress | experiment specs |
| k6 load generation | rate-capped; targets the testbed only, never a public host | load profiles |
| **Total addition** | **12 GB** | |

`make lab-up` re-reads live availability with `free -g` and **refuses to start
below 14 GB free**, so the cluster can never be the reason another tenant's
workload starts swapping.

One honest exception: k3d's `serverlb` proxy container is **not** memory-capped
— k3d applies limits to cluster nodes only. It is an nginx proxy that measures
around 90 MB in practice; it is listed here because "everything is capped"
would be a claim we cannot make.

Combined with the `dev` stack this stays under ~24 GB — inside the measured
headroom with room to spare. Nightly runs are scheduled, bounded in duration,
and torn down afterwards.

## Data retention

Unbounded telemetry growth on a shared disk is a bug, so every store has a
retention policy configured from the day it is introduced:

| Store | Policy | Where |
|---|---|---|
| VictoriaMetrics | 15 days | `-retentionPeriod` |
| Loki | 7 days, compactor-enforced | `deploy/loki/loki.yaml` |
| Tempo | 72 hours block retention | `deploy/tempo/tempo.yaml` |
| ClickHouse | per-table TTLs; system logs 7 days, verbose logs off | `deploy/clickhouse/config.d/limits.xml` |
| Redpanda | 7 days (`604800000` ms), verified for raw, normalized and DLQ topics | cluster default `log_retention_ms` |
| PostgreSQL | no TTL — incidents and the audit chain are the durable record | — |

Disk headroom is watched by meta-monitoring, with an alarm well before the
volume fills. Backups, a tested restore path, and documented RPO/RTO land with
platform hardening.

## Rules

1. Never run an unbounded stress or load job on this host.
2. Every new service arrives with a memory limit and a retention policy in the
   same change that introduces it.
3. Published ports bind to loopback and stay inside 8040–8049 (`ports.md`).
4. Re-measure with `free -g` before raising any limit, and record the change.
