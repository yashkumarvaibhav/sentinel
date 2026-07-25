#!/usr/bin/env bash
# Run the testbed's flag provider on its own, so the flag actuator can be proven
# against a real flagd without a cluster.
#
# Same image and same flag document the demo deploys - the document is extracted
# from the pinned chart rather than vendored, so there is one source of truth and
# a chart bump cannot leave this describing flags that no longer exist.
#
# One deliberate difference from the cluster: the document is mounted WRITABLE.
# In the cluster an init container copies the ConfigMap into an emptyDir at pod
# start, which is why a real flip has to roll flagd; here flagd's own file
# watcher picks the change up, so an apply and its verification can both be
# exercised against a live provider in a second rather than a minute.
#
# Port 8049, the reserved spare in our allocated 8040-8049 block, bound to
# loopback and refused if anything already holds it.
set -euo pipefail

CHART_VERSION=0.40.10
CHART=open-telemetry/opentelemetry-demo
IMAGE=ghcr.io/open-feature/flagd:v0.12.9
CONTAINER=sentinel-flagd-sandbox
OFREP_PORT=8049
STATE_DIR="${TMPDIR:-/tmp}/sentinel-flagd-sandbox"
DOCUMENT="${STATE_DIR}/demo.flagd.json"

usage() {
	echo "usage: ${0##*/} {up|down|url|document}" >&2
	exit 2
}

port_is_free() {
	! ss -tln "sport = :$1" 2>/dev/null | grep -q LISTEN
}

extract_document() {
	mkdir -p "${STATE_DIR}"
	helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
	helm template sandbox "${CHART}" --version "${CHART_VERSION}" \
		--show-only templates/flagd-config.yaml |
		python3 -c '
import sys, yaml
document = yaml.safe_load(sys.stdin)
sys.stdout.write(document["data"]["demo.flagd.json"])
' >"${DOCUMENT}"
	chmod 666 "${DOCUMENT}"
}

case "${1:-}" in
up)
	docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
	if ! port_is_free "${OFREP_PORT}"; then
		echo "error: port ${OFREP_PORT} is already in use — refusing to take it" >&2
		exit 1
	fi
	echo "extracting the flag document from ${CHART} ${CHART_VERSION}"
	extract_document
	docker run -d --name "${CONTAINER}" \
		--memory 128m --cpus 1 \
		-p "127.0.0.1:${OFREP_PORT}:8016" \
		-v "${DOCUMENT}:/etc/flagd/demo.flagd.json" \
		"${IMAGE}" start --port 8013 --ofrep-port 8016 \
		--uri file:/etc/flagd/demo.flagd.json >/dev/null
	for _ in $(seq 1 30); do
		if curl -fsS -X POST "http://127.0.0.1:${OFREP_PORT}/ofrep/v1/evaluate/flags/paymentFailure" \
			>/dev/null 2>&1; then
			echo "flagd sandbox ready — ofrep http://127.0.0.1:${OFREP_PORT}, document ${DOCUMENT}"
			exit 0
		fi
		sleep 1
	done
	echo "error: flagd did not become ready; last logs:" >&2
	docker logs --tail 30 "${CONTAINER}" >&2
	exit 1
	;;
down)
	docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
	rm -rf "${STATE_DIR}"
	echo "flagd sandbox removed"
	;;
url)
	echo "http://127.0.0.1:${OFREP_PORT}"
	;;
document)
	echo "${DOCUMENT}"
	;;
*)
	usage
	;;
esac
