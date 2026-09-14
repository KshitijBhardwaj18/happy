#!/usr/bin/env bash
# Launch the Happy demo cluster: one t3.medium Ubuntu 24.04 EC2 instance in
# the default VPC, running k3s, with the shop app manifests (infra/k8s/*.yaml)
# embedded in user-data and applied on first boot.
#
# Usage: infra/ec2-k3s/launch.sh
#
# Idempotent: if infra/ec2-k3s/.instance already points at a running/pending
# instance, this just re-prints its public IP instead of launching another.
#
# Requires: aws CLI v2 configured (default profile, or set AWS_PROFILE).

set -euo pipefail

REGION="us-east-1"
INSTANCE_TYPE="t3.medium"
KEY_NAME="happy-demo"
SG_NAME="happy-demo"
TAG_NAME="happy-demo"
AMI_SSM_PARAM="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
KEY_PATH="${HOME}/.ssh/happy-demo.pem"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$(cd "$SCRIPT_DIR/../k8s" && pwd)"
INSTANCE_FILE="$SCRIPT_DIR/.instance"
USER_DATA_TEMPLATE="$SCRIPT_DIR/user-data.sh"
USER_DATA_RENDERED="$SCRIPT_DIR/user-data.rendered.sh"

aws_() { aws --region "$REGION" "$@"; }

echo "==> Happy demo cluster launch (region ${REGION})"

# --- Idempotency: reuse an existing running/pending instance -----------------
if [[ -f "$INSTANCE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$INSTANCE_FILE"
  if [[ -n "${INSTANCE_ID:-}" ]]; then
    existing_state="$(aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" \
      --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo "gone")"
    if [[ "$existing_state" == "running" || "$existing_state" == "pending" ]]; then
      echo "==> Instance $INSTANCE_ID already $existing_state, reusing it."
      if [[ "$existing_state" == "pending" ]]; then
        aws_ ec2 wait instance-running --instance-ids "$INSTANCE_ID"
      fi
      public_ip="$(aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
      {
        echo "INSTANCE_ID=$INSTANCE_ID"
        echo "PUBLIC_IP=$public_ip"
        echo "REGION=$REGION"
      } > "$INSTANCE_FILE"
      echo "==> Public IP: $public_ip"
      exit 0
    fi
    echo "==> Previous instance $INSTANCE_ID is '$existing_state', launching a new one."
  fi
fi

# --- Default VPC and a subnet in it ------------------------------------------
VPC_ID="${VPC_ID:-$(aws_ ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)}"
if [[ -z "$VPC_ID" || "$VPC_ID" == "None" ]]; then
  echo "ERROR: no default VPC found; set VPC_ID explicitly." >&2
  exit 1
fi
echo "==> Using VPC $VPC_ID"

SUBNET_ID="${SUBNET_ID:-$(aws_ ec2 describe-subnets --filters Name=vpc-id,Values="$VPC_ID" \
  --query 'Subnets[0].SubnetId' --output text)}"
if [[ -z "$SUBNET_ID" || "$SUBNET_ID" == "None" ]]; then
  echo "ERROR: no subnet found in VPC $VPC_ID; set SUBNET_ID explicitly." >&2
  exit 1
fi
echo "==> Using subnet $SUBNET_ID"

# --- AMI: latest Ubuntu 24.04 via SSM public parameter -----------------------
AMI_ID="$(aws_ ssm get-parameter --name "$AMI_SSM_PARAM" --query 'Parameter.Value' --output text)"
echo "==> Using AMI $AMI_ID (Ubuntu 24.04, from SSM)"

# --- Key pair: create if missing ---------------------------------------------
if aws_ ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  echo "==> Key pair $KEY_NAME already exists in AWS."
  if [[ ! -f "$KEY_PATH" ]]; then
    echo "WARNING: AWS has key pair '$KEY_NAME' but $KEY_PATH is missing locally." >&2
    echo "         SSH will fail unless you have the matching private key elsewhere." >&2
  fi
else
  echo "==> Creating key pair $KEY_NAME -> $KEY_PATH"
  mkdir -p "$(dirname "$KEY_PATH")"
  aws_ ec2 create-key-pair --key-name "$KEY_NAME" --query 'KeyMaterial' --output text > "$KEY_PATH"
  chmod 600 "$KEY_PATH"
fi

# --- Security group: create + open 22/6443 if missing ------------------------
SG_ID="$(aws_ ec2 describe-security-groups \
  --filters Name=group-name,Values="$SG_NAME" Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [[ -z "$SG_ID" || "$SG_ID" == "None" ]]; then
  echo "==> Creating security group $SG_NAME"
  SG_ID="$(aws_ ec2 create-security-group --group-name "$SG_NAME" \
    --description "Happy demo cluster (SSH + k3s API, demo-only, open to 0.0.0.0/0)" \
    --vpc-id "$VPC_ID" --query 'GroupId' --output text)"
  aws_ ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --ip-permissions \
      'IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=0.0.0.0/0,Description="SSH (demo only)"}]' \
      'IpProtocol=tcp,FromPort=6443,ToPort=6443,IpRanges=[{CidrIp=0.0.0.0/0,Description="k3s API (demo only)"}]'
else
  echo "==> Security group $SG_NAME already exists ($SG_ID)."
fi
echo "==> Using security group $SG_ID"

# --- Render user-data: splice infra/k8s/*.yaml into the manifests marker ----
echo "==> Rendering user-data from $(basename "$USER_DATA_TEMPLATE") + $K8S_DIR/*.yaml"
python3 - "$USER_DATA_TEMPLATE" "$USER_DATA_RENDERED" "$K8S_DIR" <<'PYEOF'
import glob
import os
import sys

template_path, out_path, k8s_dir = sys.argv[1:4]
marker = "##HAPPY_K8S_MANIFESTS##"

blocks = []
for path in sorted(glob.glob(os.path.join(k8s_dir, "*.yaml"))):
    name = os.path.basename(path)
    delim = "HAPPY_EOF_" + name.replace(".", "_").replace("-", "_").upper()
    with open(path) as f:
        content = f.read()
    blocks.append(
        f"cat > /opt/happy/k8s/{name} <<'{delim}'\n{content}{delim}\n"
    )

template = open(template_path).read()
if marker not in template:
    sys.exit(f"marker {marker!r} not found in {template_path}")
rendered = template.replace(marker, "\n".join(blocks))

with open(out_path, "w") as f:
    f.write(rendered)
print(f"wrote {out_path} ({len(rendered)} bytes, {len(blocks)} manifests)")
PYEOF

size=$(wc -c < "$USER_DATA_RENDERED" | tr -d ' ')
if (( size > 16384 )); then
  echo "WARNING: rendered user-data is ${size} bytes, over the 16KB EC2 limit." >&2
fi

# --- Launch the instance ------------------------------------------------------
echo "==> Launching $INSTANCE_TYPE instance ($AMI_ID)"
INSTANCE_ID="$(aws_ ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --security-group-ids "$SG_ID" \
  --subnet-id "$SUBNET_ID" \
  --associate-public-ip-address \
  --user-data "file://$USER_DATA_RENDERED" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${TAG_NAME}}]" \
  --query 'Instances[0].InstanceId' --output text)"
echo "==> Instance ID: $INSTANCE_ID"

echo "==> Waiting for instance to be running..."
aws_ ec2 wait instance-running --instance-ids "$INSTANCE_ID"

PUBLIC_IP="$(aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"

{
  echo "INSTANCE_ID=$INSTANCE_ID"
  echo "PUBLIC_IP=$PUBLIC_IP"
  echo "REGION=$REGION"
} > "$INSTANCE_FILE"

echo "==> Instance running."
echo "==> Public IP: $PUBLIC_IP"
echo "==> k3s + shop app install runs via user-data; give it a few minutes, then:"
echo "      infra/ec2-k3s/fetch-kubeconfig.sh"
