#!/usr/bin/env bash
# EventBridge Scheduler -> AgentCore Runtime, no Lambda in between (universal target).
# Creates two schedules: patrol every 10 minutes, digest daily at 09:00 IST (03:30 UTC).
# Schedules start DISABLED; enable with:  ./create_schedules.sh enable
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
RUNTIME_ARN="${HAPPY_RUNTIME_ARN:-$(cd "$(dirname "$0")/../.." && agentcore status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(v for k,v in d.get("outputs",{}).items() if "RuntimeArn" in k))' 2>/dev/null || true)}"
[ -n "$RUNTIME_ARN" ] || { echo "set HAPPY_RUNTIME_ARN"; exit 1; }
ROLE_NAME="happy-scheduler-role"
GROUP="happy"
STATE="${1:-DISABLED}"; [ "$STATE" = "enable" ] && STATE="ENABLED"

if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name invoke-happy --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"bedrock-agentcore:InvokeAgentRuntime\",\"Resource\":[\"${RUNTIME_ARN}\",\"${RUNTIME_ARN}/*\"]}]}"
  sleep 10
fi
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"
aws scheduler get-schedule-group --name "$GROUP" --region "$REGION" >/dev/null 2>&1 || aws scheduler create-schedule-group --name "$GROUP" --region "$REGION" >/dev/null

mk() { # name, schedule expression, mode
  local name="$1" expr="$2" mode="$3"
  local input
  input=$(python3 -c "import json,sys; print(json.dumps({'AgentRuntimeArn': sys.argv[1], 'RuntimeSessionId': '${mode}-<aws.scheduler.execution-id>', 'Qualifier': 'DEFAULT', 'Payload': json.dumps({'mode': '${mode}'})}))" "$RUNTIME_ARN")
  local target
  target=$(python3 -c "import json,sys; print(json.dumps({'Arn':'arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime','RoleArn':sys.argv[1],'Input':sys.argv[2],'RetryPolicy':{'MaximumRetryAttempts':0}}))" "$ROLE_ARN" "$input")
  if aws scheduler get-schedule --name "$name" --group-name "$GROUP" --region "$REGION" >/dev/null 2>&1; then verb=update-schedule; else verb=create-schedule; fi
  aws scheduler $verb --name "$name" --group-name "$GROUP" --region "$REGION" --schedule-expression "$expr" --flexible-time-window Mode=OFF --state "$STATE" --target "$target" >/dev/null
  echo "$verb $name ($expr) state=$STATE"
}
mk happy-patrol "rate(10 minutes)" patrol
mk happy-digest "cron(30 3 * * ? *)" digest
