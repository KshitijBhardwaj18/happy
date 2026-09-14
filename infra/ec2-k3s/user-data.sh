#!/bin/bash
# EC2 user-data for the Happy demo cluster.
#
# This file is a TEMPLATE: launch.sh renders it by splicing the contents of
# infra/k8s/*.yaml into the marker below (as heredocs writing files under
# /opt/happy/k8s) and passes the *rendered* copy as --user-data. Do not run
# this file as-is; run launch.sh instead.
#
# What this does, on first boot as root:
#   1. Discovers this instance's public IP via the IMDSv2 metadata service.
#   2. Writes the shop app's k8s manifests to /opt/happy/k8s (see marker).
#   3. Installs k3s as a single-node server, telling it to put the public IP
#      in the API server's TLS SAN list so a kubeconfig rewritten to use that
#      IP (see fetch-kubeconfig.sh) validates correctly.
#   4. Waits for the node to report Ready.
#   5. kubectl apply -f /opt/happy/k8s
#
# Everything is logged to /var/log/happy-user-data.log for debugging.

set -euxo pipefail
exec > >(tee -a /var/log/happy-user-data.log) 2>&1

echo "happy user-data: starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- 1. Public IP via IMDSv2 -------------------------------------------------
IMDS_TOKEN="$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")"
PUBLIC_IP="$(curl -sf -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  "http://169.254.169.254/latest/meta-data/public-ipv4")"
echo "happy user-data: public IP is ${PUBLIC_IP}"

# --- 2. Write the shop app manifests -----------------------------------------
mkdir -p /opt/happy/k8s

##HAPPY_K8S_MANIFESTS##

# --- 3. Install k3s, TLS SAN = public IP -------------------------------------
curl -sfL https://get.k3s.io | \
  INSTALL_K3S_EXEC="server --tls-san ${PUBLIC_IP} --write-kubeconfig-mode 644" sh -

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
export PATH="/usr/local/bin:${PATH}"

# --- 4. Wait for the node to be Ready ----------------------------------------
echo "happy user-data: waiting for node Ready"
for i in $(seq 1 60); do
  if kubectl get nodes 2>/dev/null | grep -qE '\sReady\s'; then
    echo "happy user-data: node is Ready"
    break
  fi
  sleep 5
done
kubectl get nodes || true

# --- 5. Apply the shop app manifests -----------------------------------------
echo "happy user-data: applying /opt/happy/k8s manifests"
kubectl apply -f /opt/happy/k8s

echo "happy user-data: done at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
