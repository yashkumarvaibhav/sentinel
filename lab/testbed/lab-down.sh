#!/usr/bin/env bash
# Tear the testbed down. `stop` keeps the cluster and its images for the next
# run; `delete` reclaims everything.
set -euo pipefail

CLUSTER=sentinel-lab
MODE="${1:-stop}"

if ! k3d cluster list "${CLUSTER}" >/dev/null 2>&1; then
	echo "cluster '${CLUSTER}' does not exist — nothing to do"
	exit 0
fi

case "${MODE}" in
stop)
	k3d cluster stop "${CLUSTER}"
	echo "stopped — 'make lab-up' restarts it; 'make lab-destroy' removes it"
	;;
delete)
	k3d cluster delete "${CLUSTER}"
	echo "deleted"
	;;
*)
	echo "usage: $0 [stop|delete]" >&2
	exit 1
	;;
esac
