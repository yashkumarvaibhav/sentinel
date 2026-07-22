# Scenario symptom-label capabilities

Private scenario labels are accepted only when a public, contained stimulus has
an independently verifiable effect on the current detector input surface. The
compiler rejects arbitrary answer keys before a workload can run.

| Public stimulus | Required support traffic | Label capability | Evidence used by runtime |
|---|---|---|---|
| fixed-target single-path k6 attack, at most 20 rps | normal primary event traffic | `RESIDUAL_EXCEED frontend/request_rate`; `RATIO_DEFORM frontend/path_entropy` | measured frontend server-span count, path and event time |
| `paymentFailure=100%` through flagd | overlapping fixed checkout journey, at most 5 iterations/s | `EDGE_DEGRADED checkout/dependency.payment`; `LOG_BURST payment/log_template_rate` | trace-correlated dependency calls and service-local log records |

The path attack is included in the capture's primary-volume correlation using a
child user-agent identity. The exact child identity separately measures the
stimulus interval; replay accepts the root plus its children without admitting
unrelated workload traffic. Combined primary and attack load is capped at 50
rps. Every stimulus-backed residual or symptom interval is rewritten from the
measured execution timestamps before the private capture artifact is sealed.

`combo_night` development seeds are 503/509 and held-out seeds are 9209/9221.
The committed profile currently has honest positive labels for residual, ratio,
log and dependency-edge evidence. `SATURATION`, `DROP` and `SILENCE` remain
explicitly unsupported in this profile until their public testbed stimuli and
detector inputs are proven; no placeholder labels are emitted for them.
