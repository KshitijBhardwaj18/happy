#!/usr/bin/env bash
# Fetch the k3s kubeconfig from the Happy demo instance, point it at the
# instance's public IP instead of 127.0.0.1, write it to ~/.kube/happy.yaml,
# and stash a base64 copy as KUBECONFIG_B64 in the main happy checkout's .env.
#
# Usage: infra/ec2-k3s/fetch-kubeconfig.sh
#
# Never prints the kubeconfig or its base64 value (it's a cluster-admin
# credential) -- only paths and status.

set -euo pipefail

KEY_PATH="${HOME}/.ssh/happy-demo.pem"
LOCAL_KUBECONFIG="${HOME}/.kube/happy.yaml"
MAIN_ENV_FILE="/Users/kshitij/development/happy/.env"
SSH_USER="ubuntu"
SSH_OPTS=(-i "$KEY_PATH" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o BatchMode=yes)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTANCE_FILE="$SCRIPT_DIR/.instance"

if [[ -f "$INSTANCE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$INSTANCE_FILE"
fi
PUBLIC_IP="${1:-${PUBLIC_IP:-}}"
if [[ -z "${PUBLIC_IP:-}" ]]; then
  echo "ERROR: no public IP. Run launch.sh first, or pass it as \$1." >&2
  exit 1
fi
if [[ ! -f "$KEY_PATH" ]]; then
  echo "ERROR: SSH key $KEY_PATH not found. Run launch.sh first." >&2
  exit 1
fi

echo "==> Waiting for SSH on ${SSH_USER}@${PUBLIC_IP}..."
deadline=$((SECONDS + 300))
until ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" true 2>/dev/null; do
  if (( SECONDS > deadline )); then
    echo "ERROR: SSH did not come up within 5 minutes." >&2
    exit 1
  fi
  sleep 5
done
echo "==> SSH is up."

echo "==> Waiting for k3s kubeconfig to exist on the instance..."
deadline=$((SECONDS + 300))
until ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" \
    'test -r /etc/rancher/k3s/k3s.yaml' 2>/dev/null; do
  if (( SECONDS > deadline )); then
    echo "ERROR: /etc/rancher/k3s/k3s.yaml never appeared (check user-data log via" >&2
    echo "       ssh ${SSH_USER}@${PUBLIC_IP} sudo cat /var/log/happy-user-data.log)" >&2
    exit 1
  fi
  sleep 5
done

echo "==> Fetching kubeconfig..."
tmp_kubeconfig="$(mktemp)"
trap 'rm -f "$tmp_kubeconfig"' EXIT
scp "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}:/etc/rancher/k3s/k3s.yaml" "$tmp_kubeconfig" >/dev/null

mkdir -p "$(dirname "$LOCAL_KUBECONFIG")"
sed "s/127\.0\.0\.1/${PUBLIC_IP}/g" "$tmp_kubeconfig" > "$LOCAL_KUBECONFIG"
chmod 600 "$LOCAL_KUBECONFIG"
echo "==> Wrote $LOCAL_KUBECONFIG (server rewritten to https://${PUBLIC_IP}:6443)"

KUBECONFIG_B64="$(base64 < "$LOCAL_KUBECONFIG" | tr -d '\n')"

mkdir -p "$(dirname "$MAIN_ENV_FILE")"
touch "$MAIN_ENV_FILE"
if grep -q '^KUBECONFIG_B64=' "$MAIN_ENV_FILE" 2>/dev/null; then
  tmp_env="$(mktemp)"
  awk -v val="KUBECONFIG_B64=${KUBECONFIG_B64}" \
    '{ if ($0 ~ /^KUBECONFIG_B64=/) print val; else print }' \
    "$MAIN_ENV_FILE" > "$tmp_env"
  mv "$tmp_env" "$MAIN_ENV_FILE"
  echo "==> Replaced KUBECONFIG_B64 line in $MAIN_ENV_FILE"
else
  printf 'KUBECONFIG_B64=%s\n' "$KUBECONFIG_B64" >> "$MAIN_ENV_FILE"
  echo "==> Appended KUBECONFIG_B64 line to $MAIN_ENV_FILE"
fi
unset KUBECONFIG_B64

echo "==> Done. Try: kubectl --kubeconfig $LOCAL_KUBECONFIG get nodes"
