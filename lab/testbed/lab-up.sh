#!/usr/bin/env bash
# Bring the evaluation testbed up: a k3s cluster (via k3d) on the same isolated
# docker network as the rest of the stack.
#
# Refuses to start rather than guessing: if the stack is not up, the cluster has
# no network to join and nothing to export telemetry to.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${REPO_ROOT}/lab/testbed/k3d-cluster.yaml"
CLUSTER=sentinel-lab
NETWORK=sentinel_net

need() {
	command -v "$1" >/dev/null || {
		echo "error: $1 is not installed" >&2
		exit 1
	}
}

need k3d
need kubectl
need docker

if ! docker network inspect "${NETWORK}" >/dev/null 2>&1; then
	echo "error: docker network '${NETWORK}' does not exist — run 'make up' first" >&2
	exit 1
fi

# The cluster is sized from docs/resource-budget.md, but the budget is a
# snapshot of a shared box. Check reality before claiming 12 GB of it.
available_gb=$(free -g | awk '/^Mem:/ {print $7}')
required_gb=14
if ((available_gb < required_gb)); then
	echo "error: only ${available_gb} GB available, testbed needs ~${required_gb} GB" >&2
	echo "       other workloads on this host come first — retry later" >&2
	exit 1
fi

if k3d cluster list "${CLUSTER}" >/dev/null 2>&1; then
	echo "cluster '${CLUSTER}' already exists — starting it if stopped"
	k3d cluster start "${CLUSTER}" >/dev/null
else
	echo "creating cluster '${CLUSTER}' (${available_gb} GB available)"
	k3d cluster create --config "${CONFIG}"
fi

kubectl --context "k3d-${CLUSTER}" wait --for=condition=Ready nodes --all --timeout=180s

echo
kubectl --context "k3d-${CLUSTER}" get nodes -o wide
echo
echo "kube API: https://127.0.0.1:8047 (loopback only)"
echo "context : k3d-${CLUSTER}   —   kubectl --context k3d-${CLUSTER} get pods -A"
