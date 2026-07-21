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
make lab-deploy  # mesh + Chaos Mesh + network containment, wired to our collector
make lab-status  # nodes and workloads
make lab-chaos   # inject a fault (EXPERIMENT=name)
make lab-load    # generate load (PROFILE=, RATE=, DURATION=)
make lab-undeploy # remove the mesh, keep the cluster
make lab-down    # stop, keeping the cluster and its images
make lab-destroy # delete it entirely
```

`make lab-deploy` is idempotent and brings up everything the lab needs: the
instrumented mesh, Chaos Mesh for fault injection, and the network policies that
contain it.

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

## Perturbing it: load and chaos

The lab is only useful if it produces the incidents the product is graded on,
in the shapes real outages take.

- **Load** (`make lab-load`) runs a k6 profile as an in-cluster Job. It is
  rate-capped (default 5 rps, hard ceiling 50) and its target is fixed to the
  in-cluster frontend — a load generator that can be aimed anywhere is one typo
  from being aimed at something that is not ours. `lab/loadgen/baseline.js` is
  the starter profile.
- **Chaos** (`make lab-chaos`) applies a Chaos Mesh experiment from
  `lab/testbed/chaos/`. The starter, `ad-cpu-pressure`, drives CPU load into one
  service's own cgroup — it cannot pressure the host, and it self-expires.
  Experiments are CRDs in version control, not dashboard clicks.

Both were verified to move real telemetry into our stores: a 20 rps k6 run took
span-metric call volume from ~140 to ~9,800 calls/min, and the CPU experiment
took the ad service's p95 latency from ~6 ms to over 4,000 ms — read from
VictoriaMetrics, not from the tools that caused them.

## Containment

- The API server binds `127.0.0.1:8047` only, inside our allocated block.
- No NodePort services — the port range is deliberately narrowed and unused.
- No ingress controller and no service load balancer.
- **Egress is default-deny** (`lab/testbed/network-policy.yaml`, applied by
  `make lab-deploy`): testbed pods reach each other, cluster DNS, and the OTLP
  ports on our docker network — nothing else. A pod in the mesh cannot reach the
  public internet by name *or* by raw IP; this was tested, not assumed. So
  "load and chaos target testbed services only" is enforced by the cluster, not
  by everyone remembering to be careful.

## Verifying it is real

The point of the testbed is that its telemetry is emergent, not authored. The
check that matters is end-to-end: a signal produced by a workload in the
cluster, read back out of our own stores.

```bash
# metrics — a business counter that climbs while the load generator runs
curl -s --data-urlencode 'query=sum(app_ads_ad_requests_total)' \
  http://127.0.0.1:8042/api/v1/query

# logs — real application lines, not synthetic ones
docker run --rm --network sentinel_net curlimages/curl -s -G \
  'http://loki:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={service_name=~"checkout|cart|payment"}' --data-urlencode 'limit=3'

# traces — find one, then read its spans; a real trace crosses services
docker run --rm --network sentinel_net curlimages/curl -s -G \
  'http://tempo:3200/api/search' --data-urlencode 'tags=service.name=frontend'
docker run --rm --network sentinel_net curlimages/curl -s "http://tempo:3200/api/traces/<id>"
```

If metrics look stale, check the age of the last sample rather than assuming
the pipeline is broken — an instant query returns nothing when the newest
sample is older than the lookbehind window, which is what a restarting
collector looks like:

```bash
curl -s --data-urlencode 'query=time() - timestamp(last_over_time(app_ads_ad_requests_total[1h]))' \
  http://127.0.0.1:8042/api/v1/query
```

## The application

**OpenTelemetry Demo (Astronomy Shop)**, helm chart `0.40.10` / app `2.2.0`,
deployed as release `astronomy` in namespace `otel-demo` — 22 pods across 18
services (a Go/Java/.NET/Python/Node/Rust/PHP mix, all natively instrumented),
with Kafka, Postgres and Valkey behind them and a load generator producing
continuous real traffic.

Two components are switched off: the bundled `llm` model server (out of scope,
and the heaviest pod in the mesh) with the `product-reviews` service that
depends on it, and the `flagd-ui` sidecar (OOM-killed at the chart's 250Mi
limit; we drive flags through the API, not a second web app).

## Capability matrix

The scenarios we score against need specific things to exist in the mesh. This
is the evidence for or against locking in this application — every row was
checked against the running deployment, not the documentation.

| Capability | Needed for | Verdict |
|---|---|---|
| Trace coverage across services | critical-path RCA, origin localization | **Yes.** A single sampled trace spans `frontend-proxy → cart → valkey-cart`; Tempo reports spans from 14+ distinct services |
| Feature flags with failure modes | code/config fault injection; the flag actuator | **Yes, richly.** 15 flags in `flagd`, several *graded* rather than binary: `paymentFailure` (10–100%), `emailMemoryLeak` (1x–10000x), `imageSlowLoad` (5s/10s). Also `adHighCpu`, `adFailure`, `cartFailure`, `productCatalogFailure`, `paymentUnreachable`, `kafkaQueueProblems`, `failedReadinessProbe` |
| Retry / failure hooks | cascade profiles; retry-storm detection | **Yes.** `recommendationCacheFailure` and `kafkaQueueProblems` produce genuine downstream cascades; Envoy at `frontend-proxy` supplies retry behaviour |
| Checkout / conversion signal | business-impact estimate; "users restored" rollback proof | **Yes.** `app_confirmation_counter_total`, `app_payment_transactions_total`, `app_frontend_requests_total`, plus cart latency histograms |
| Deploy / rollout events | change correlation as evidence | **Yes.** Kubernetes rollout events plus the flagd change feed — both real, both observable |
| Built-in traffic flood | the "event explains volume" scenario | **Yes.** `loadGeneratorFloodHomepage` multiplies request rate without deforming behavioural ratios — exactly the legitimate-surge shape the product must *not* call an attack |
| Auth-like flow (login / credential path) | credential-stuffing profile; auth-failure-rate ratio | **No.** The shop has no login, no session, no credential endpoint |
| Protected-cohort metric | guards — proving an action did not harm a protected group | **No.** No cohort dimension exists in the emitted telemetry |

### Decision

**Lock it in.** Six of eight capabilities are present and real, including the
two hardest to fake: cross-service trace coverage and a graded fault-injection
surface. The alternatives considered (Google Online Boutique, Train-Ticket)
lose on instrumentation quality, and neither has graded flags.

The two gaps are real and are recorded here rather than glossed:

- **No auth flow.** The credential-stuffing profile cannot use a real login
  path. It must instead be driven against an existing POST endpoint, with the
  behavioural ratio expressed as error-rate and session-churn deformation
  rather than auth-failure rate. That is a scenario-design constraint to settle
  when the attack profiles are built — not a reason to reject the mesh.
- **No protected cohort.** The load generator must tag personas (via header →
  baggage → resource attribute) to create a cohort dimension we control. That
  lands with `config/cohorts.yml`.

Both gaps affect *scenario authoring*, not the telemetry pipeline — which is
what this phase had to prove.
