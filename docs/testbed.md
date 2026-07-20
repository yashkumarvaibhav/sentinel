# The testbed

Sentinel is graded on telemetry from a **real instrumented service mesh** under
real load and real faults — not on scripted numbers. That mesh runs in a
Kubernetes cluster on this host.

## What it is

| | |
|---|---|
| Distribution | k3s `v1.31.14-k3s1`, run in docker via **k3d** |
| Cluster name | `sentinel-lab` (context `k3d-sentinel-lab`) |
| Topology | 1 server + 1 agent |
| Network | joins `sentinel_net` — the same isolated network as the stack |
| API server | `https://127.0.0.1:8047` — **loopback only** |
| Memory cap | 4 GB server + 8 GB agent (`docs/resource-budget.md`, `nightly-lab`) |

**Why k3d and not a host k3s install:** we have no root on this shared box, and
a containerised cluster is easier to contain. Joining `sentinel_net` means pods
reach our collector directly, so the testbed needs **no extra host port** —
`ARCHITECTURE.md` §6 names this as the sanctioned alternative.

Traefik, servicelb and metrics-server are disabled: each wants host ports we
have not been allocated, and nothing in the testbed should be reachable except
through us.

## Bring-up and teardown

```bash
make up          # the stack first — the cluster joins its network
make lab-up      # create (or restart) the cluster, wait for nodes Ready
make lab-status  # nodes and workloads
make lab-down    # stop, keeping the cluster and its images
make lab-destroy # delete it entirely
```

`make lab-up` refuses to run if `sentinel_net` does not exist, and refuses if
the host has less than 14 GB of RAM available. Other people's workloads share
this machine; a testbed that starves them is a failure, not a trade-off. The
check reads live availability, not the snapshot in the budget doc.

## Reaching the stack from inside the cluster

k3d injects the docker network's members into CoreDNS, so pods resolve our
containers by name:

```bash
kubectl --context k3d-sentinel-lab run netcheck --rm -i --restart=Never \
  --image=curlimages/curl:latest --command -- \
  curl -s -o /dev/null -w '%{http_code}\n' http://sentinel-otel-collector-1:13133/
# 200
```

Those records are written when the cluster is created, so recreate the cluster
after recreating the stack, or the addresses go stale.

## Containment

- The API server binds `127.0.0.1:8047` only, inside our allocated block.
- No NodePort services — the port range is deliberately narrowed and unused.
- No ingress controller and no service load balancer.
- Load generation and fault injection target testbed services only; there is no
  path from a lab job to a public host, and adding one is not a configuration
  choice we are willing to make.

## Verifying it is real

The point of the testbed is that its telemetry is emergent, not authored. The
check that matters is end-to-end: a signal produced by a workload in the
cluster, read back out of our own stores.

```bash
# metric
curl -s 'http://127.0.0.1:8042/api/v1/query?query=<metric>' | head -c 300
# logs
docker run --rm --network sentinel_net curlimages/curl -s -G \
  'http://loki:3100/loki/api/v1/query_range' --data-urlencode 'query={service_name="<svc>"}'
# trace
docker run --rm --network sentinel_net curlimages/curl -s "http://tempo:3200/api/traces/<id>"
```

## Capability matrix

The scenarios we score against need specific things to exist in the mesh. This
table is the evidence for or against locking in the current demo application —
filled in when the mesh is deployed.

| Capability | Needed for | Present? |
|---|---|---|
| Auth-like flow (login / credential path) | credential-stuffing attack profile; auth-failure-rate ratio | _pending deployment_ |
| Checkout / conversion signal | business-impact estimate; "users restored" rollback proof | _pending deployment_ |
| Feature flags with failure modes | code/config fault injection; the flag actuator | _pending deployment_ |
| Retry / failure hooks | cascade profiles; retry-storm detection | _pending deployment_ |
| Protected-cohort metric | guard rails — proving an action did not harm a protected group | _pending deployment_ |
| Trace coverage across services | critical-path RCA, origin localization | _pending deployment_ |
| Deploy / rollout events | change correlation as evidence | _pending deployment_ |
