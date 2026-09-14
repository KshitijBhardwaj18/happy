#!/usr/bin/env bash
# Tear down the Happy demo cluster: terminate the instance, delete the
# security group, and delete the key pair (AWS-side and the local .pem).
#
# Usage: infra/ec2-k3s/teardown.sh

set -euo pipefail

REGION="us-east-1"
KEY_NAME="happy-demo"
SG_NAME="happy-demo"
KEY_PATH="${HOME}/.ssh/happy-demo.pem"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTANCE_FILE="$SCRIPT_DIR/.instance"

aws_() { aws --region "$REGION" "$@"; }

echo "==> Tearing down Happy demo cluster (region ${REGION})"

INSTANCE_ID=""
if [[ -f "$INSTANCE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$INSTANCE_FILE"
fi

if [[ -n "${INSTANCE_ID:-}" ]]; then
  state="$(aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo "gone")"
  if [[ "$state" != "gone" && "$state" != "terminated" ]]; then
    echo "==> Terminating instance $INSTANCE_ID (state: $state)"
    aws_ ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null
    echo "==> Waiting for termination..."
    aws_ ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
    echo "==> Instance terminated."
  else
    echo "==> Instance $INSTANCE_ID already $state."
  fi
else
  echo "==> No instance recorded in $INSTANCE_FILE; skipping instance termination."
fi

# --- Security group (must wait until nothing is using it) -------------------
VPC_ID="${VPC_ID:-$(aws_ ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text 2>/dev/null || true)}"
SG_ID="$(aws_ ec2 describe-security-groups \
  --filters Name=group-name,Values="$SG_NAME" Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [[ -n "$SG_ID" && "$SG_ID" != "None" ]]; then
  echo "==> Deleting security group $SG_NAME ($SG_ID)"
  # ENI detachment after instance termination can lag by a few seconds.
  for i in $(seq 1 12); do
    if aws_ ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null; then
      echo "==> Security group deleted."
      break
    fi
    sleep 5
  done
else
  echo "==> Security group $SG_NAME not found; skipping."
fi

# --- Key pair (AWS-side and local private key) -------------------------------
if aws_ ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  echo "==> Deleting key pair $KEY_NAME"
  aws_ ec2 delete-key-pair --key-name "$KEY_NAME" >/dev/null
else
  echo "==> Key pair $KEY_NAME not found in AWS; skipping."
fi
if [[ -f "$KEY_PATH" ]]; then
  rm -f "$KEY_PATH"
  echo "==> Removed local $KEY_PATH"
fi

rm -f "$INSTANCE_FILE"
echo "==> Teardown complete."
