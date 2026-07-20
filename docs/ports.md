# Ports

This host is shared with other applications. Sentinel is allocated the
**8040–8049** block and must never bind outside it, and never touch another
application's service, port, or data.

Every published port binds to **127.0.0.1**. Nothing here needs LAN or WAN
exposure: the public hostname is served by a Cloudflare tunnel that dials the
front door on loopback. Loosening a binding is a decision to record, not a
convenience.

## The block

| Port | Service | Published by | Status |
|---|---|---|---|
| 8040 | API gateway (FastAPI: REST + SSE; `/ws` reserved) | compose | planned |
| 8041 | Caddy front door — serves the web build, proxies `/api`, `/stream`, `/ws` → 8040 | compose | planned |
| 8042 | VictoriaMetrics | compose | live |
| 8043 | Grafana | compose | live |
| 8044 | Keycloak (OIDC) | compose | planned |
| 8045 | Redpanda console (dev only) | compose | live |
| 8046 | MLflow UI | compose | planned |
| 8047 | Testbed Kubernetes API (k3s via k3d) | `make lab-up` | live |
| 8048 | Vite dev server | local dev only | live |
| 8049 | Vite preview server | local dev only | live |

## Deliberately not published

These are reachable only inside the `sentinel_net` docker network:

| Service | Internal endpoint |
|---|---|
| PostgreSQL | `postgres:5432` |
| ClickHouse | `clickhouse:8123` (HTTP), `clickhouse:9000` (native) |
| Redpanda | `redpanda:9092` (Kafka API) |
| Loki | `loki:3100` |
| Tempo | `tempo:3200`, `tempo:4317` (OTLP) |
| OTel Collector | `otel-collector:4317` / `:4318` (OTLP), `:13133` (health) |

The k3s testbed keeps its API server on loopback (8047, above) and runs no
NodePort services, no ingress controller and no service load balancer — so
"cluster-internal" is enforced rather than assumed. ClickHouse HTTP, which
8047 was previously held for, is never exposed to the host; it is reachable
only inside `sentinel_net`.

## Ports belonging to other applications

Never bind, proxy, or reconfigure these — they belong to other apps on the box:
22, 53, 4010, 5433, 5544, 8002, 8011, 8021, 8031, 8888, 8890. The Cloudflare
tunnel and its configuration are owner-operated.

## Checking

```bash
ss -tln | grep -E ':804[0-9]'     # what Sentinel currently holds
docker compose -p sentinel ps      # which of ours are up
```
