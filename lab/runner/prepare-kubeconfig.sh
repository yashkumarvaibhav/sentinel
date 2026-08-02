#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
target="${repo_root}/var/demo-runner-kubeconfig"
mkdir -p "${repo_root}/var"
temporary="$(mktemp "${target}.XXXXXX")"

cleanup() {
  rm -f "${temporary}"
}
trap cleanup EXIT

if k3d kubeconfig get sentinel-lab >"${temporary}"; then
  kubectl config --kubeconfig "${temporary}" set-cluster k3d-sentinel-lab \
    --server=https://k3d-sentinel-lab-serverlb:6443 >/dev/null
  mv "${temporary}" "${target}"
elif [[ ! -f "${target}" ]]; then
  # The runner still serves deterministic replay with no cluster. An empty
  # kubeconfig makes LIVE preflight fail clearly without preventing startup.
  : >"${target}"
  echo "sentinel: testbed kubeconfig unavailable; replay remains ready, live is disabled" >&2
fi
chmod 600 "${target}"
