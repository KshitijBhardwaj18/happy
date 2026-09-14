permit(principal,
  action == AgentCore::Action::"Hands___scale_deployment",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:575751781540:gateway/happy-happyhands-uacr1x3qd2")
when { context.input.replicas <= 5 };
