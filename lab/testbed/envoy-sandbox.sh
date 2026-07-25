#!/usr/bin/env bash
# Run the vendored edge proxy on its own, so the mesh actuator can be proven
# against a real Envoy without a cluster.
#
# This is not a second testbed. It is the same image the demo runs
# (ghcr.io/open-telemetry/demo:<tag>-frontend-proxy) reading the same template
# lab-deploy.sh mounts into the cluster, which is what makes a proof here a
# proof about the real edge: the binary, the config and the runtime layers are
# the same, only the upstreams are absent.
#
# The frontend cluster is pointed at Envoy's own admin listener so a request
# that survives the rate limit gets a real HTTP answer. That makes the two
# outcomes we care about unambiguous - 429 means the cohort was restrained,
# anything else means it was not.
#
# Ports: 8048/8049, the reserved spare pair in our allocated 8040-8049 block,
# bound to loopback and refused if anything already holds them.
set -euo pipefail

IMAGE_TAG="${SENTINEL_ENVOY_TAG:-2.2.0}"
IMAGE="ghcr.io/open-telemetry/demo:${IMAGE_TAG}-frontend-proxy"
CONTAINER=sentinel-envoy-sandbox
PROXY_PORT=8048
ADMIN_PORT=8049
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="${REPO_ROOT}/lab/testbed/envoy.tmpl.yaml"

usage() {
	echo "usage: ${0##*/} {up|down|url}" >&2
	exit 2
}

port_is_free() {
	! ss -tln "sport = :$1" 2>/dev/null | grep -q LISTEN
}

case "${1:-}" in
up)
	docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
	for port in "${PROXY_PORT}" "${ADMIN_PORT}"; do
		if ! port_is_free "${port}"; then
			echo "error: port ${port} is already in use — refusing to take it" >&2
			exit 1
		fi
	done
	docker run -d --name "${CONTAINER}" \
		--memory 256m --cpus 1 \
		-p "127.0.0.1:${PROXY_PORT}:8080" \
		-p "127.0.0.1:${ADMIN_PORT}:10000" \
		-v "${TEMPLATE}:/home/envoy/envoy.tmpl.yaml:ro" \
		-e ENVOY_PORT=8080 \
		-e ENVOY_ADDR=0.0.0.0 \
		-e ENVOY_ADMIN_PORT=10000 \
		-e OTEL_SERVICE_NAME=frontend-proxy \
		-e OTEL_COLLECTOR_HOST=127.0.0.1 \
		-e OTEL_COLLECTOR_PORT_GRPC=4317 \
		-e OTEL_COLLECTOR_PORT_HTTP=4318 \
		-e FRONTEND_HOST=127.0.0.1 \
		-e FRONTEND_PORT=10000 \
		-e IMAGE_PROVIDER_HOST=127.0.0.1 \
		-e IMAGE_PROVIDER_PORT=8081 \
		-e FLAGD_HOST=127.0.0.1 \
		-e FLAGD_PORT=8013 \
		-e FLAGD_UI_HOST=127.0.0.1 \
		-e FLAGD_UI_PORT=4000 \
		-e LOCUST_WEB_HOST=127.0.0.1 \
		-e LOCUST_WEB_PORT=8089 \
		-e GRAFANA_HOST=127.0.0.1 \
		-e GRAFANA_PORT=3000 \
		-e JAEGER_HOST=127.0.0.1 \
		-e JAEGER_UI_PORT=16686 \
		"${IMAGE}" >/dev/null
	for _ in $(seq 1 30); do
		if curl -fsS "http://127.0.0.1:${ADMIN_PORT}/ready" >/dev/null 2>&1; then
			echo "envoy sandbox ready — admin http://127.0.0.1:${ADMIN_PORT}"
			exit 0
		fi
		sleep 1
	done
	echo "error: envoy did not become ready; last logs:" >&2
	docker logs --tail 30 "${CONTAINER}" >&2
	exit 1
	;;
down)
	docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
	echo "envoy sandbox removed"
	;;
url)
	echo "http://127.0.0.1:${ADMIN_PORT}"
	;;
*)
	usage
	;;
esac
