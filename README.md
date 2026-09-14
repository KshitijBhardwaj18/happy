# Happy

**The on-call junior who never sleeps and never touches prod without asking.**

Happy is a background SRE agent built with the Strands Agents SDK and deployed on Amazon Bedrock AgentCore.
It patrols a Kubernetes cluster, investigates incidents with a team of specialist agents, fixes safe things
itself, asks in Slack before risky things, is hard-blocked from dangerous things by AgentCore Policy, and learns
from every incident: fingerprinting failures, remembering what fixed them, and opening postmortem and runbook
pull requests in your repo.

Built for the AWS "Agents for Humans" hackathon, Professional Agents track. MIT licensed.

_Full README, architecture diagram and setup guide land with the docs PR. See `docs/plan/prs.md` for the build plan._
