#!/usr/bin/env bash
# Chaos toggles for the Happy demo cluster (namespace: shop).
#
# Usage: chaos.sh {oom|crashloop|badimage|errors|reset} [service]
#
#   oom        LEAK_MB=400 on checkout (or [service])       -> OOMKilled within ~60s (256Mi limit)
#   crashloop  CRASH_ON_START=true on inventory (or [service]) -> CrashLoopBackOff
#   badimage   image -> python:3.12-slim-doesnotexist on frontend (or [service]) -> ImagePullBackOff
#   errors     ERROR_RATE=0.5 on checkout (or [service])     -> ~50% 500s from GET /
#   reset      clear chaos env vars, restore images, re-apply infra/k8s manifests
#
# Environment:
#   KUBECONFIG   path to kubeconfig (default: $HOME/.kube/happy.yaml)

set -euo pipefail

KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/happy.yaml}"
NAMESPACE="shop"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$(cd "$SCRIPT_DIR/../infra/k8s" && pwd)"

kube() {
  kubectl --kubeconfig "$KUBECONFIG_PATH" "$@"
}

usage() {
  cat <<EOF
Usage: $(basename "$0") {oom|crashloop|badimage|errors|reset} [service]

  oom        LEAK_MB=400 on checkout (or [service])          -> OOMKilled within ~60s (256Mi limit)
  crashloop  CRASH_ON_START=true on inventory (or [service])  -> CrashLoopBackOff
  badimage   image -> python:3.12-slim-doesnotexist on frontend (or [service]) -> ImagePullBackOff
  errors     ERROR_RATE=0.5 on checkout (or [service])        -> ~50% 500s from GET /
  reset      clear chaos env vars, restore images, re-apply infra/k8s manifests

Environment:
  KUBECONFIG   path to kubeconfig (default: \$HOME/.kube/happy.yaml)
EOF
}

require_deployment() {
  local svc="$1"
  if ! kube get deployment "$svc" -n "$NAMESPACE" >/dev/null 2>&1; then
    echo "chaos.sh: deployment '$svc' not found in namespace '$NAMESPACE' (did you kubectl apply -f infra/k8s?)" >&2
    exit 1
  fi
}

cmd="${1:-}"
service_override="${2:-}"

case "$cmd" in
  oom)
    svc="${service_override:-checkout}"
    require_deployment "$svc"
    echo "chaos.sh: setting LEAK_MB=400 on deployment/$svc (expect OOMKilled within ~60s)"
    kube set env "deployment/$svc" -n "$NAMESPACE" LEAK_MB=400
    ;;
  crashloop)
    svc="${service_override:-inventory}"
    require_deployment "$svc"
    echo "chaos.sh: setting CRASH_ON_START=true on deployment/$svc (expect CrashLoopBackOff)"
    kube set env "deployment/$svc" -n "$NAMESPACE" CRASH_ON_START=true
    ;;
  badimage)
    svc="${service_override:-frontend}"
    require_deployment "$svc"
    echo "chaos.sh: setting image python:3.12-slim-doesnotexist on deployment/$svc (expect ImagePullBackOff)"
    kube set image "deployment/$svc" -n "$NAMESPACE" "$svc=python:3.12-slim-doesnotexist"
    ;;
  errors)
    svc="${service_override:-checkout}"
    require_deployment "$svc"
    echo "chaos.sh: setting ERROR_RATE=0.5 on deployment/$svc (expect ~50% 500s from GET /)"
    kube set env "deployment/$svc" -n "$NAMESPACE" ERROR_RATE=0.5
    ;;
  reset)
    echo "chaos.sh: clearing chaos env vars and restoring images on frontend, checkout, inventory"
    for svc in frontend checkout inventory; do
      kube set env "deployment/$svc" -n "$NAMESPACE" LEAK_MB- CRASH_ON_START- ERROR_RATE- LATENCY_MS- >/dev/null 2>&1 || true
      kube set image "deployment/$svc" -n "$NAMESPACE" "$svc=python:3.12-slim" >/dev/null 2>&1 || true
    done
    echo "chaos.sh: re-applying infra/k8s manifests"
    kube apply -f "$K8S_DIR"
    ;;
  -h|--help|help|"")
    usage
    exit 0
    ;;
  *)
    echo "chaos.sh: unknown command '$cmd'" >&2
    usage
    exit 1
    ;;
esac
