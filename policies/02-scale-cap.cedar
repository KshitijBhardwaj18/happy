permit(principal,
  action == AgentCore::Action::"Hands___scale_deployment",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__")
when { context.input.replicas <= 5 };
