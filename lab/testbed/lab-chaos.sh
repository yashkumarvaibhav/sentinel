#!/usr/bin/env bash
# Run a chaos experiment against the testbed and wait for it to finish.
#
# EXPERIMENT=<name> selects a file from lab/testbed/chaos/.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTEXT=k3d-sentinel-lab
EXPERIMENT="${EXPERIMENT:-ad-cpu-pressure}"
MANIFEST="${REPO_ROOT}/lab/testbed/chaos/${EXPERIMENT}.yaml"

[[ -f "${MANIFEST}" ]] || {
	echo "error: no experiment '${EXPERIMENT}' — available:" >&2
	basename -s .yaml -a "${REPO_ROOT}"/lab/testbed/chaos/*.yaml >&2
	exit 1
}

kubectl --context "${CONTEXT}" -n chaos-mesh get deploy chaos-controller-manager >/dev/null 2>&1 || {
	echo "error: Chaos Mesh is not installed — run 'make lab-deploy' first" >&2
	exit 1
}

kind=$(awk '/^kind:/ {print $2; exit}' "${MANIFEST}")
duration=$(awk '/^  duration:/ {print $2; exit}' "${MANIFEST}")

echo "applying ${kind}/${EXPERIMENT} (duration ${duration})"
kubectl --context "${CONTEXT}" apply -f "${MANIFEST}"

# Experiments are self-expiring, but leaving one applied means the next run
# inherits it. Clean up on the way out, however we exit.
trap 'kubectl --context "${CONTEXT}" delete -f "${MANIFEST}" --ignore-not-found >/dev/null 2>&1' EXIT

kubectl --context "${CONTEXT}" -n otel-demo wait "${kind}/${EXPERIMENT}" \
	--for=condition=AllInjected --timeout=90s
echo "injected — running for ${duration}"

sleep "$(python3 -c "
import re,sys
d='${duration}'
m=re.fullmatch(r'(\d+)([smh])', d)
print(int(m.group(1)) * {'s':1,'m':60,'h':3600}[m.group(2)] if m else 120)
")"

echo "done — the effect should be visible in metrics and traces for that window"
