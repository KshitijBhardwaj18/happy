# Agents for Humans: Building Happy, an on-call agent that never touches prod without asking

I'm building Happy for the AWS "Agents for Humans" hackathon, and the idea started from a
complaint, not a feature list. On-call is mostly not deciding things. It's checking things. Did
the pod restart. Is the error rate up. Did that deploy from an hour ago line up with when things
went sideways. Should I roll back. Every one of those is a five-minute task, and none of them
individually deserves your full attention, but stacked up across a shift they're the whole shift.
The actual judgment calls — the ones that need a person — are maybe 10% of it.

So Happy is built to do the other 90%. It sits next to a Kubernetes cluster, checks it every ten
minutes, and only talks to you in Slack when there's something worth knowing or a decision only
you should make. The pitch I keep coming back to: the on-call junior who never sleeps and never
touches prod without asking. This post is about how it's put together and what building it on
Amazon Bedrock AgentCore with the Strands Agents SDK actually felt like, warts included.

## What it does

A scheduled patrol reads deployment health across a watched namespace. If everything's fine, it
posts nothing — silence is a feature, not an omission. If something's unhealthy, it pulls logs
and events, fingerprints the failure (more on that in post three), and either recognizes it from
a past incident or spins up a small team of specialist agents to work out what happened. Once it
has a proposed fix, that fix sits on one of three tiers: safe things it just does, risky things
it asks about in Slack and waits for a reply, and a couple of things it is flatly not allowed to
do, enforced by policy outside its own code (post two covers that boundary in depth). Resolved
incidents get a postmortem PR. Fixes that have worked twice get a runbook PR.

## The shape of it

```
EventBridge Scheduler ──▶ AgentCore Runtime (Happy, Strands)
                              ├── reads ──▶ k3s API on EC2 (pods, logs, events)
                              ├── writes ─▶ AgentCore Gateway ──▶ Lambda "happy-hands" ──▶ k3s API
                              │                 └── Policy engine (Cedar): forbid delete, cap scale
                              ├── GitHub API (commits, PRs, postmortem PR)   token via Identity
                              ├── Slack (digests, approvals, audit thread)   token via Identity
                              ├── Code Interpreter (log analysis)
                              └── Memory + CloudWatch traces + Evaluations
```

EventBridge Scheduler invokes the AgentCore Runtime with a plain JSON payload —
`{"mode": "patrol"}` or `{"mode": "digest"}`. The agent reads the cluster directly, because reads
can't hurt anything. Every write goes the long way around: through an MCP client to an AgentCore
Gateway, which is the only thing allowed to reach a Lambda that actually talks to Kubernetes, and
that Gateway has a Cedar policy engine sitting in front of it. That detour is the whole point of
the architecture — it's not there because it was convenient, it's there so that "don't delete the
namespace" is a rule an AWS service enforces, not a sentence in a system prompt.

## Building it with the AgentCore CLI

I hadn't used `@aws/agentcore` before this. `agentcore create --project-name happy --name happy
--language Python --framework Strands --model-provider Bedrock --memory long-term --build
CodeZip` scaffolds a project with a flat resource model: agents, memories, gateways, policy
engines, and evaluators all live as independent top-level arrays in one `agentcore.json`, with no
explicit bindings between them in the schema. That took a minute to get used to — I kept looking
for a place to wire a memory *to* a runtime — but it's actually a clean model once it clicks: a
runtime discovers its memory ID and gateway URL at run time, from environment variables the CLI
injects, rather than through a static reference in the config. Here's the shape of the memory
resource, four strategies on one memory store:

```json
"memories": [
  {
    "name": "happyMemory",
    "eventExpiryDuration": 30,
    "strategies": [
      { "type": "SEMANTIC", "namespaceTemplates": ["/users/{actorId}/facts"] },
      { "type": "USER_PREFERENCE", "namespaceTemplates": ["/users/{actorId}/preferences"] },
      { "type": "SUMMARIZATION", "namespaceTemplates": ["/summaries/{actorId}/{sessionId}"] },
      { "type": "EPISODIC", "namespaceTemplates": ["/episodes/{actorId}/{sessionId}"],
        "reflectionNamespaceTemplates": ["/episodes/{actorId}"] }
    ]
  }
]
```

`CodeZip` builds mean no Docker in the loop for the agent itself — the CLI zips `app/happy/`,
uploads it, and the Runtime handles the rest. The generated `main.py` gave me a working
`BedrockAgentCoreApp` entrypoint, an MCP client stub, and a model loader on day one, which was a
genuinely good starting point: I had `agentcore deploy` producing a runtime that would answer a
prompt before I'd written a single tool. What I ended up throwing away was the generated single
inline `add_numbers` tool and the `NullConversationManager` — those are placeholders meant to be
replaced, and the project structure makes it obvious where.

The part that took longest wasn't the agent code, it was the Gateway. A Gateway target needs a
tool schema (`tools.json`) that mirrors your Lambda's dispatch logic exactly, and the action names
Cedar sees later are `<TargetName>___<toolName>` — that underscore convention isn't obvious until
you've read a rejected policy statement once or twice. Once I understood it, wiring
`rollout_restart`, `scale_deployment`, `rollback_deployment`, `set_image`, and the deliberately
forbidden `delete_namespace` into one Lambda behind one Gateway target was mechanical:

```python
TOOLS = {
    "rollout_restart": rollout_restart,
    "scale_deployment": scale_deployment,
    "rollback_deployment": rollback_deployment,
    "set_image": set_image,
    "delete_namespace": delete_namespace,
}

def handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full = custom.get("bedrockAgentCoreToolName", "") or (event or {}).get("tool", "")
    tool = full.split("___")[-1]
    fn = TOOLS.get(tool)
    ...
```

## What I'd change

Being straight about the current state: the Runtime is deployed with `networkMode: "PUBLIC"`, not
inside a VPC alongside the cluster, and the Gateway's authorizer is `NONE` for the demo — I cover
both, and what production would look like instead, in the README's security notes. The
investigation Graph, the patrol/digest runner, and the postmortem/runbook writer are built on
feature branches headed into `main`, not sitting there today — better to say that plainly than
imply a finished system. What I like about the CLI so far is that none of that is architectural
debt: the flat resource model means adding the Graph or wiring Identity later doesn't touch the
Gateway or Policy config at all. They're independent pieces, which is exactly what let this get
split into parallel pull requests against one `agentcore.json`.

Next up: why the parts of Happy that say no live outside the model entirely, and what that
actually buys you over a well-worded system prompt.
