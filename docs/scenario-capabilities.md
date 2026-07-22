# Scenario symptom-label capabilities

Private scenario labels are accepted only when a public, contained stimulus has
an independently verifiable effect on the current detector input surface. The
compiler rejects arbitrary answer keys before a workload can run.

| Public stimulus | Required support traffic | Label capability | Evidence used by runtime |
|---|---|---|---|
| fixed-target single-path k6 attack, at most 20 rps | normal primary event traffic | `RESIDUAL_EXCEED frontend/request_rate`; `RATIO_DEFORM frontend/path_entropy` | measured frontend server-span count, path and event time |
| `paymentFailure=100%` through flagd | overlapping fixed checkout journey, at most 5 iterations/s | `EDGE_DEGRADED checkout/dependency.payment`; `LOG_BURST` on payment, checkout, cart and frontend | trace-correlated dependency calls and service-local propagated error records |
| sustained fixed checkout journey | primary workload remains separately correlated | `SATURATION checkout/container_memory` | checkout working-set plus its fixed Kubernetes hard limit |
| low-rate primary phase under an active event expectation | exact load phase match | `DROP frontend/request_rate` | low-but-present frontend ingress spans and context-aware expected volume |
| bounded `frontend-proxy-pod-failure` | primary fixed-target workload continues | `SILENCE frontend/request_rate` | complete schedule watermark and missing frontend ingress evidence |

The path attack is included in the capture's primary-volume correlation using a
child user-agent identity. The exact child identity separately measures the
stimulus interval; replay accepts the root plus its children without admitting
unrelated workload traffic. Combined primary and attack load is capped at 50
rps. Every stimulus-backed residual or symptom interval is rewritten from the
measured execution timestamps before the private capture artifact is sealed.

`combo_night` development seeds are 503/509 and held-out seeds are 9209/9221.
The committed profile has evidence-authored positive labels for all seven
runtime symptom kinds. Silence advances on the complete public schedule
watermark; replay never converts a missing span bucket into a fabricated zero
measurement. The compiler still rejects any label whose exact stimulus,
service and signal capability is absent.
