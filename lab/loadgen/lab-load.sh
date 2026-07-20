#!/usr/bin/env bash
# Run a k6 load profile against the testbed, as a Job inside the cluster.
#
# In-cluster on purpose: the job can only reach services the testbed's network
# policy allows, so "targets the testbed only" is enforced by the cluster rather
# than by everyone remembering to be careful.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTEXT=k3d-sentinel-lab
NAMESPACE=otel-demo
PROFILE="${PROFILE:-baseline}"
SCRIPT="${REPO_ROOT}/lab/loadgen/${PROFILE}.js"
JOB="k6-${PROFILE}"

RATE="${RATE:-5}"
DURATION="${DURATION:-2m}"
MAX_RATE=50

[[ -f "${SCRIPT}" ]] || {
	echo "error: no profile '${PROFILE}' — available:" >&2
	basename -s .js -a "${REPO_ROOT}"/lab/loadgen/*.js >&2
	exit 1
}

# The cap is the point. This box runs other people's workloads; an uncapped
# load generator here is an outage for them, not a bigger number for us.
if ((RATE > MAX_RATE)); then
	echo "error: RATE=${RATE} exceeds the ${MAX_RATE} rps cap for this host" >&2
	echo "       raising it is a decision to record, not a flag to pass" >&2
	exit 1
fi

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" delete job "${JOB}" --ignore-not-found >/dev/null
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" delete configmap "${JOB}" --ignore-not-found >/dev/null
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create configmap "${JOB}" --from-file="script.js=${SCRIPT}"

echo "running ${PROFILE} at ${RATE} rps for ${DURATION}"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB}
  labels:
    sentinel.dev/role: loadgen
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    metadata:
      labels:
        sentinel.dev/role: loadgen
    spec:
      restartPolicy: Never
      containers:
        - name: k6
          image: grafana/k6:0.55.0
          args: ["run", "/scripts/script.js"]
          env:
            - name: RATE
              value: "${RATE}"
            - name: DURATION
              value: "${DURATION}"
          resources:
            limits:
              cpu: "1"
              memory: 256Mi
            requests:
              cpu: 100m
              memory: 64Mi
          volumeMounts:
            - name: script
              mountPath: /scripts
      volumes:
        - name: script
          configMap:
            name: ${JOB}
YAML

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" wait --for=condition=Ready pod -l job-name="${JOB}" --timeout=120s
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" logs -f "job/${JOB}"
