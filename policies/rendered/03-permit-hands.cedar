permit(principal,
  action in [AgentCore::Action::"Hands___rollout_restart",
             AgentCore::Action::"Hands___rollback_deployment",
             AgentCore::Action::"Hands___set_image"],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:575751781540:gateway/happy-happyhands-uacr1x3qd2");
