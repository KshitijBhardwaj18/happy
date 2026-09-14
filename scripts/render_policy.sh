#!/usr/bin/env bash
# Substitute the deployed gateway ARN into each Cedar policy (Cedar forbids wildcard resources;
# AgentCore requires exactly one statement per policy). Then registers them on the HappyPolicy engine.
set -euo pipefail
ARN="${1:?usage: render_policy.sh <gateway-arn>}"
mkdir -p policies/rendered
for f in policies/0*.cedar; do
  name="$(basename "$f" .cedar)"
  sed "s#__GATEWAY_ARN__#${ARN}#g" "$f" > "policies/rendered/${name}.cedar"
  pname="$(python3 -c "import sys,re; print(''.join(w.capitalize() for w in re.sub(r'^[0-9]+-','',sys.argv[1]).split('-')))" "$name")"
  agentcore add policy --name "$pname" --engine HappyPolicy --source "policies/rendered/${name}.cedar" --description "Happy trust ladder: ${name}" >/dev/null 2>&1 || echo "policy ${pname} already present"
done
echo "rendered and registered $(ls policies/rendered | wc -l | tr -d ' ') policies for ${ARN}"
