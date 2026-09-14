#!/usr/bin/env bash
# Substitute the deployed gateway ARN into the Cedar policy (Cedar forbids wildcard resources).
set -euo pipefail
ARN="${1:?usage: render_policy.sh <gateway-arn>}"
sed "s#__GATEWAY_ARN__#${ARN}#g" policies/happy.cedar > policies/happy.rendered.cedar
echo "rendered policies/happy.rendered.cedar for ${ARN}"
