#!/usr/bin/env bash
# Store the demo cluster kubeconfig (as JSON) in SSM so the Gateway Lambda can reach the cluster.
set -euo pipefail
KUBECONFIG_FILE="${1:-$HOME/.kube/happy.yaml}"
PARAM="${HAPPY_KUBECONFIG_PARAM:-/happy/kubeconfig}"
kubectl --kubeconfig "$KUBECONFIG_FILE" config view --raw -o json > /tmp/happy-kubeconfig.json
aws ssm put-parameter --name "$PARAM" --type SecureString --overwrite --value "file:///tmp/happy-kubeconfig.json" >/dev/null
rm -f /tmp/happy-kubeconfig.json
echo "kubeconfig stored in SSM parameter $PARAM"
