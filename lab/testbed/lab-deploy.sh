#!/usr/bin/env bash
# Deploy the instrumented mesh into the testbed cluster.
#
# The demo supplies the application; we supply the observability. Its telemetry
# leaves the cluster over OTLP to our collector, which is what makes every
# signal downstream of here real rather than authored.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VALUES="${REPO_ROOT}/lab/testbed/otel-demo-values.yaml"
CONTEXT=k3d-sentinel-lab
NAMESPACE=otel-demo
RELEASE=astronomy
CHART=open-telemetry/opentelemetry-demo
CHART_VERSION=0.40.10
COLLECTOR_CONTAINER=sentinel-otel-collector-1

kubectl --context "${CONTEXT}" cluster-info >/dev/null 2>&1 || {
	echo "error: cluster '${CONTEXT}' is not reachable — run 'make lab-up' first" >&2
	exit 1
}

# The demo exports to our collector by container name. That name only resolves
# inside the cluster because k3d wrote the docker network's members into
# CoreDNS when the cluster was created — so a collector that has been recreated
# since then resolves to a stale address. Fail early and loudly instead of
# debugging silence later.
if ! docker ps --format '{{.Names}}' | grep -qx "${COLLECTOR_CONTAINER}"; then
	echo "error: ${COLLECTOR_CONTAINER} is not running — run 'make up' first" >&2
	exit 1
fi
live_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${COLLECTOR_CONTAINER}")
dns_ip=$(kubectl --context "${CONTEXT}" -n kube-system get cm coredns \
	-o jsonpath='{.data.NodeHosts}' | awk -v n="${COLLECTOR_CONTAINER}" '$2 == n {print $1}')
if [[ "${live_ip}" != "${dns_ip}" ]]; then
	echo "error: cluster DNS has ${COLLECTOR_CONTAINER} at '${dns_ip}', it is actually at '${live_ip}'" >&2
	echo "       the stack was recreated after the cluster — run 'make lab-destroy && make lab-up'" >&2
	exit 1
fi

CHAOS_VALUES="${REPO_ROOT}/lab/testbed/chaos-mesh-values.yaml"
CHAOS_VERSION=2.8.3

helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
helm repo update open-telemetry chaos-mesh >/dev/null

echo "deploying ${CHART} ${CHART_VERSION} as ${RELEASE}/${NAMESPACE}"
helm --kube-context "${CONTEXT}" upgrade --install "${RELEASE}" "${CHART}" \
	--version "${CHART_VERSION}" \
	--namespace "${NAMESPACE}" --create-namespace \
	--values "${VALUES}" \
	--timeout 15m \
	--wait

# Fault injection is part of the lab, not an optional add-on — the incidents the
# product is graded on come from here.
echo "deploying Chaos Mesh ${CHAOS_VERSION}"
helm --kube-context "${CONTEXT}" upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
	--version "${CHAOS_VERSION}" \
	--namespace chaos-mesh --create-namespace \
	--values "${CHAOS_VALUES}" \
	--timeout 10m \
	--wait

# Containment travels with the mesh, not as a separate step someone has to
# remember: the lab exists to generate attacks and faults, so its blast radius
# is closed the moment it comes up.
echo "applying network containment"
kubectl --context "${CONTEXT}" apply -f "${REPO_ROOT}/lab/testbed/network-policy.yaml"

echo
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" get pods
