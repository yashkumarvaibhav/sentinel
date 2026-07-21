# Scenario scoring

`make score` is the Phase 1 held-out gate. It compiles the committed
`quiet_day` and `match_night` profiles, runs every held-out seed as a bounded k6
Job inside the `otel-demo` namespace, waits for the Collector/Redpanda/ingest
path, and reads the matching real frontend-proxy ingress spans back from
ClickHouse. A unique non-label user agent correlates each workload run to its
telemetry; the target URL remains hard-coded inside the k6 script and cannot be
supplied by scenario data.

The compiler writes three artifacts under ignored `var/scoring/` state:

- `schedule.json`: deterministic, rate-capped k6 phases;
- `context-feed.json`: relative event windows and expected total multipliers;
- `private/labels.json`: residual answer intervals, consumed only by scoring.

Runtime decomposition receives only canonical `Observation` records derived
from real ingress-span counts and `ContextWindow` records. The evaluator applies
private labels only after the engine has emitted predictions. An AST import
test prevents `detection`, `decision`, `rca`, or `action` from importing any
`lab` or label module, while the runtime telemetry contract rejects known
answer-key fields and attributes.

The first versioned gates are residual precision >= 0.90, residual recall >=
0.90, quiet-day false-positive rate = 0, and at least 0.95 telemetry
completeness for every run. Empty denominators report `insufficient`; they never
become fake zeroes. Detection latency uses nearest-rank p50/p95. Decision,
reason, origin, action and resolution metrics stay explicitly `insufficient`
until their owning phases exist.

The proof report is regenerated at
`docs/reports/phase-1-decomposition-score.md`. Both the workload and scenario
context are labeled **SIMULATED**; only the instrumented testbed telemetry is
labeled **REAL**. Phase 1.9 records these live runs at the raw-bus boundary so
future hosted score and golden gates can replay them bit-exactly without k3s.

The scorer-only capture path is `make score-captures
CAPTURE_ROOT=<directory>`. It discovers verified capture directories, requires
the matrix to match all four committed held-out `(profile, seed)` pairs exactly,
replays normalization and decomposition without labels, and only then verifies
and applies `private/labels.json`. Missing, duplicate or extra runs fail before
gate evaluation. This diagnostic target remains separate from `make score`
until the DVC-backed matrix is clone-accessible and hosted CI can fetch it.
