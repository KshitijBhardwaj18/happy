// Happy's trust ladder, enforced by AgentCore Policy at the Gateway (not by the model).
// Action names are "<Target>___<tool>"; the resource is the deployed gateway ARN,
// substituted by scripts/render_policy.sh after the first deploy.

// Never, no matter what the model decides.
forbid(principal,
  action == AgentCore::Action::"Hands___delete_namespace",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:575751781540:gateway/happy-happyhands-uacr1x3qd2");

// Scaling is safe only within a small band.
permit(principal,
  action == AgentCore::Action::"Hands___scale_deployment",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:575751781540:gateway/happy-happyhands-uacr1x3qd2")
when { context.input.replicas <= 5 };

// Safe and approval-gated actions are allowed at the gateway; the approval gate itself
// lives in the agent (a Strands interrupt that waits for a human reply in Slack).
permit(principal,
  action in [AgentCore::Action::"Hands___rollout_restart",
             AgentCore::Action::"Hands___rollback_deployment",
             AgentCore::Action::"Hands___set_image"],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:575751781540:gateway/happy-happyhands-uacr1x3qd2");
