# Agents for Humans: the trust ladder — interrupts, hooks, and Cedar policy in one agent

The first version of Happy's safety story was a paragraph in the system prompt: "never delete a
namespace, always ask before rolling back." That lasted about an hour before I threw it out. A
system prompt is a strong suggestion the model reads and, most of the time, follows — but "most
of the time" is not a sentence I want near a command that deletes a namespace. This post is about
where I moved the "no" instead: partly into the agent's own code, and partly out of the agent
entirely, into an AWS policy engine that never sees a prompt.

## Three tiers, one dictionary

Every write Happy can make against the cluster sits on exactly one tier: `SAFE` (just do it and
say so afterwards), `APPROVAL` (pause, ask in Slack, wait for a reply), or `FORBIDDEN` (never,
full stop). The whole ladder is one dictionary:

```python
TIERS: dict[str, Tier] = {
    "rollout_restart": "SAFE",
    "scale_deployment": "SAFE",  # only up to Settings.safe_scale_max; above that -> APPROVAL
    "rollback_deployment": "APPROVAL",
    "set_image": "APPROVAL",
    "delete_namespace": "FORBIDDEN",
}
```

Restarting a stuck pod can't make things worse than they already are, so it's `SAFE`. Rolling
back or swapping the running image changes what's serving traffic, so those ask first. Deleting a
namespace is irreversible and has no legitimate use in an on-call flow, so it's `FORBIDDEN` — the
tool exists so the demo can prove it's blocked, not because Happy should ever need it.

## Interrupts: pausing an agent mid-thought

Strands has a primitive for exactly the `APPROVAL` case: `tool_context.interrupt(...)`. Calling it
from inside a tool stops the agent loop — `AgentResult.stop_reason` comes back as `"interrupt"` —
and hands control back to whatever's driving the agent, carrying a payload describing what needs
a decision. Here's the whole function that raises one:

```python
def require_approval(tool_context: ToolContext, action: str, details: dict) -> tuple[bool, str]:
    response = tool_context.interrupt("approval", reason={"action": action, "details": details})
    approved = isinstance(response, str) and response.strip().lower().startswith("approve")
    return approved, response
```

What makes this more than a fancy `input()` call is that the pause survives a restart. The
pending interrupt lives in the agent's session, and the session is backed by AgentCore Memory —
so if the process handling this invocation dies while waiting on a human, a fresh invocation with
the same session id picks the conversation back up mid-interrupt instead of losing the incident.
The loop that drives this end to end lives in `approvals.py`:

```python
result = agent(prompt)
while result.stop_reason == "interrupt":
    responses = []
    for interrupt in result.interrupts:
        short_id = _short_id(interrupt.id)
        action = (interrupt.reason or {}).get("action", "action")
        details = (interrupt.reason or {}).get("details", {})
        after_ts = post_message(
            f'🟡 Approval needed [{short_id}]: {action} {details}. '
            f'Reply "approve {short_id}" or "deny {short_id} <reason>".',
            thread_ts=thread_ts,
        )
        reply = wait_for_reply(pattern=re.escape(short_id), after_ts=after_ts, ...)
        responses.append({"interruptResponse": {"interruptId": interrupt.id, "response": reply}})
    result = agent(responses)
```

One thing I didn't expect to like as much: a `deny` with a reason longer than a few words gets
remembered as a preference (`ledger.remember_preference`), not just logged and discarded. "Don't
roll back checkout during business hours," said once, becomes context for every future proposal
on that service. It doesn't change what tier anything sits on — a preference shapes what Happy
*proposes*, never what it's allowed to do without asking.

## Hooks: the audit trail and a local backstop

Strands hooks fire around every tool call regardless of which tool it is, which makes them the
right place for two things that have nothing to do with any single tool's logic: writing an audit
line, and enforcing the `FORBIDDEN` tier even when nothing else in the path would catch it.

```python
class PolicyHook(HookProvider):
    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._enforce)

    def _enforce(self, event: BeforeToolCallEvent) -> None:
        name = _tool_name(event.tool_use)
        tier = tier_for(name, event.tool_use.get("input"))
        if tier == "FORBIDDEN":
            event.cancel_tool = f"Blocked by policy: {name} is FORBIDDEN for Happy"
```

`AuditHook` mirrors this on `AfterToolCallEvent`: every write call, whatever its outcome, becomes
one JSON line on disk and one message in the incident's Slack thread. Read-only lookups are
skipped on purpose — the audit trail is about what Happy *did*, not everything it looked at.

I want to be honest about `PolicyHook`: it's in-process Python. If that process is compromised, or
something calls the cluster-write functions from a path that skips the hook, it does nothing.
That's exactly why it isn't the real answer.

## The real answer is outside the process

Every write Happy makes doesn't call Kubernetes directly — it calls an AgentCore Gateway over
MCP, and the Gateway is the only thing with credentials to the Lambda that touches the cluster.
Sitting in front of that Gateway is an AgentCore Policy engine evaluating Cedar rules on every
single call, independent of the agent process, independent of the model, independent of
`PolicyHook`. AgentCore requires one statement per policy, so the trust ladder at this layer is
three small files:

```
forbid(principal,
  action == AgentCore::Action::"Hands___delete_namespace",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__");
```

```
permit(principal,
  action == AgentCore::Action::"Hands___scale_deployment",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__")
when { context.input.replicas <= 5 };
```

The scale cap is the detail I like most, because it shows the two layers aren't redundant copies
of each other — they enforce different things for different reasons. The in-process tier bumps a
scale request above 5 replicas to `APPROVAL`, so a human gets asked. The Cedar rule only `permit`s
scale calls when replicas are 5 or fewer, full stop, no exception for an approved request. If
someone approves scaling to 20 in Slack, the call still reaches the Gateway and is still refused —
the boundary that matters was never told approval was a way around the cap. `delete_namespace`
gets the same treatment from the other direction: `forbid` always wins over `permit` in Cedar, so
no rule anywhere could accidentally let it through.

## Why the boundary has to be outside the model

None of this is about distrusting Claude specifically. A model making the "should I delete this"
decision, however well-instructed, is one probability distribution away from getting it wrong
once — and once is enough for a namespace. Moving the hard "no" to a policy engine that evaluates
a Cedar rule against a gateway action name changes the failure mode entirely: an attacker or a
confused model can get the agent to *try* the call, and the call still doesn't execute. The demo
beat here is almost anticlimactic on purpose — ask Happy to delete the namespace, watch the
Gateway log a deny, watch nothing happen to the cluster. That's the point. Boring is what safety
is supposed to look like.

Next: how Happy remembers incidents across days without that memory ever becoming a way to skip
the approval it just asked for.
