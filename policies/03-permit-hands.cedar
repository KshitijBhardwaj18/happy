permit(principal,
  action in [AgentCore::Action::"Hands___rollout_restart",
             AgentCore::Action::"Hands___rollback_deployment",
             AgentCore::Action::"Hands___set_image"],
  resource == AgentCore::Gateway::"__GATEWAY_ARN__");
