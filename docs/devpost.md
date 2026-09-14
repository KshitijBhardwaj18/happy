# Devpost submission text

## Short blurb (~60 words)

Happy is an on-call agent for teams running Kubernetes. It patrols your cluster every 10 minutes,
investigates failures with specialist sub-agents, fixes safe problems itself, and asks in Slack
before anything risky. Dangerous actions are blocked by an AWS policy engine outside the model.
It remembers past incidents, spots repeats faster, and writes postmortems and runbooks into your
repo.

## Text description (Devpost)

**What it does**

Happy is the on-call junior who never sleeps and never touches prod without asking. It runs on a
schedule next to a Kubernetes cluster and handles the routine 90% of on-call: checking what
broke, working out why, fixing the safe things, and asking a human before the risky ones. A
morning digest at 9:00 summarizes what happened overnight. A patrol every 10 minutes checks every
service; healthy means silence, unhealthy means investigation. Every proposed fix sits on one of
three tiers — SAFE it just does, APPROVAL it pauses on and asks about in Slack, and FORBIDDEN it
cannot do at all, because that block lives outside its own code, enforced by an AWS policy engine
between the agent and the cluster. Every incident is fingerprinted from the shape of its errors,
so a repeat is recognized even when pod names and timestamps differ, and the fix that worked
before is proposed again, with evidence, still behind the same approval gate. Resolved incidents
get a postmortem pull request; fixes that have worked twice get a runbook pull request — the
knowledge leaves the agent and lands in the team's repo.

**Who it's for**

Small teams and solo developers who run their own Kubernetes cluster and carry the pager for
it — no dedicated SRE org, no maintained runbook wiki, but still wanting that discipline.

**How it works**

Happy is a [Strands Agents SDK](https://strandsagents.com) application deployed on [Amazon
Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/). EventBridge Scheduler invokes the
AgentCore Runtime on a schedule with a payload like `{"mode": "patrol"}`. The agent reads the
cluster directly through the Kubernetes API. Every write that changes the cluster — restart,
scale, rollback, image change, and the deliberately forbidden namespace delete — is instead an
MCP tool call to an AgentCore Gateway, governed by an AgentCore Policy engine evaluating Cedar
rules before the call reaches the Lambda that talks to Kubernetes. When an action needs a human,
a Strands interrupt pauses the agent mid-run and posts to Slack; the run resumes exactly where it
stopped once a person replies, even after a restart, because the pending state lives in the
agent's session on AgentCore Memory. A Graph of specialist sub-agents investigates in parallel —
one reads logs with AgentCore Code Interpreter, one reads cluster events, one checks git history
for the likely cause — and a synthesizer turns their findings into a structured incident report.
An incident ledger, also on AgentCore Memory, fingerprints failures, tracks outcomes, and turns a
denial-with-a-reason into a standing preference. CloudWatch records every run as a trace, and
AgentCore Evaluations scores tool selection and helpfulness.

**AWS and Strands features used**

AgentCore: Runtime, Memory (four strategies — semantic, user preference, summarization,
episodic), Gateway, Policy, Code Interpreter, Observability, and Evaluations, plus Identity
planned for the two external tokens. Strands: `@tool` functions, `BeforeToolCallEvent` /
`AfterToolCallEvent` hooks for audit and a local policy fallback, `tool_context.interrupt` for
human-in-the-loop approvals, Graph multi-agent orchestration with per-node model providers, a
summarizing conversation manager, session management, structured output via Pydantic models, and
an MCP client talking to the Gateway. Surrounding AWS: EventBridge Scheduler, Lambda as the
Gateway's compute target, and CloudWatch Transaction Search.

**What's next**

The investigation Graph, the patrol/digest runner, and the postmortem/runbook writer are in
progress on feature branches, headed for `main`. After that: wiring AgentCore Identity credential
providers so Slack and GitHub tokens are fetched per-call instead of read from environment
variables, moving the Gateway to `CUSTOM_JWT` authorization instead of `NONE`, putting the
Runtime in a VPC alongside the cluster, and extending the trust ladder as more of the cluster
becomes writable by the agent.
