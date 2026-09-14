# Happy: the on-call engineer's background helper

Hackathon: Agents for Humans, Professional Agents track. Deadline: today, Sep 14 2026, 5:00 PM PDT (5:30 AM IST Sep 15). Repo: github.com/KshitijBhardwaj18/happy (public, empty, MIT to add). Agent name: Happy.

---

## Part 0: Narrative

**One-liner.** Happy is the on-call junior who never sleeps and never touches prod without asking.

**Hook (first 20 s of the video).** On-call is 90% checking and 10% deciding. Happy does the 90%, gets faster at it every week, and never gets more reckless.

**Five differentiators, each a demo beat**
1. Its "no" lives outside the model: forbidden actions are Cedar rules enforced by AgentCore Policy at the Gateway. Demo: delete namespace → deny in Gateway log.
2. It learns to diagnose faster, never to skip you: fingerprints incidents, recognises repeats, proposes last working fix, still asks. Demo: same failure twice → "seen before, fixed in 4 minutes."
3. Knowledge leaves the agent: postmortem PR after an incident, runbook PR after a fix works twice. Demo: PR opens in the repo.
4. Silence is the feature: healthy patrol posts nothing; one message = one decision; audit in the thread. Demo: the Slack channel is the whole UI.
5. The SRE agent has an SRE: CloudWatch traces + AgentCore Evaluations scores. Demo: 20 s on trace and scores.

**Judging map.** Creativity: 1, 2, 3. Design: 4 (Slack UX, no dashboard). Impact: toil hours and time-to-recovery. Technical: 8/10 AgentCore + Strands depth, each load-bearing. Presentation: six beats, camera in Slack and terminal, consoles only at the end.

**Blog series (bonus 0.2 each, titles include "Agents for Humans").**
1. Building Happy, an on-call agent that never touches prod without asking (story + architecture).
2. The trust ladder: interrupts, hooks and AgentCore Policy in one agent (safety deep dive).
3. Teaching an agent to remember incidents: fingerprinting with AgentCore Memory (learning loop).

**Narrative risk.** Too much console time makes it feel like an infra demo, not an Agents for Humans entry. Keep consoles to 30 s.

---

## Part 1: Spec (plain language, point form)

### The problem
- On-call engineers lose their day to small repeated checks: what broke, why, did the deploy cause it, should I roll back, who do I tell.
- Each check is five minutes. Together they eat the day and crowd out the real judgment calls.

### Who it is for
- Small teams and solo developers who run a Kubernetes cluster and are on call for their own services.

### What Happy is
- A background agent that lives next to your cluster and does the routine parts of on-call.
- You never open it. It runs on a schedule and only messages you in Slack when there is something worth knowing or a decision only a human should make.
- Pitch line: "the on-call junior who never sleeps and never touches prod without asking."

### What Happy does, feature by feature
1. **Morning digest.** At 9:00 it posts what broke overnight, what it fixed alone, what needs a human, and which pull requests are waiting.
2. **Remembers across days.** It recalls earlier incidents and your preferences, so it can say "checkout ran out of memory again, third time this week."
3. **Patrols every 10 minutes.** Checks every service. Healthy means silence. Unhealthy means it investigates.
4. **Investigates like a team.** Three specialists work in parallel: one reads logs, one reads cluster events, one checks what changed in git. A lead writes the incident report.
5. **Crunches logs properly.** The log specialist runs real analysis code in a sandbox, so it can say "errors went from 0% to 40% at 03:12" instead of guessing from a few lines.
6. **Finds the cause in git.** It matches the moment things broke to the pull request that was deployed just before, and names it.
7. **Fixes safe things itself.** Restarting a service or scaling it up a little. It tells you afterwards.
8. **Asks before risky things.** Rolling back or changing the running version. It pauses, posts "approve" or "deny" in Slack, and waits. When you reply it continues exactly where it stopped, even if it was restarted in between.
9. **Cannot do dangerous things.** Deleting a namespace is blocked by a rule that lives outside the agent's own code, in AWS. Even if the model tries, the call never reaches the cluster.
10. **Keeps an audit trail.** Every action is logged and mirrored into the Slack thread of the incident.
11. **Writes the postmortem.** When an incident is resolved, Happy opens a pull request in your repo with a postmortem document: timeline, cause, fix, follow-ups.
12. **Writes the handoff note.** At end of shift it posts open incidents, what was tried, and what to watch.
13. **Keeps no secrets in its config.** Slack and GitHub tokens live in AWS's credential vault, fetched at run time.
14. **Is watched itself.** Every run is recorded as a trace in CloudWatch, and built-in evaluators score whether it picked the right tools and gave useful answers.
15. **Never learns to skip approvals.** Approving once does not make the next one automatic. Humans stay in the loop by design.

### How Happy learns (the self-learning loop)
16. **Fingerprints every incident.** It strips timestamps, ids and numbers from error logs, keeps the shape of the errors plus the service and the failure reason, and turns that into a short signature. Same failure, same signature, even if the pod names and times differ.
17. **Recognises repeats.** On every patrol it compares the new signature with its incident ledger. If it has seen it before it says so: "This matches incident #3, seen twice. Last time a rollback fixed it in 4 minutes." It proposes the fix that worked before, first. It still asks for approval if that fix is risky.
18. **Learns from outcomes, not just from you.** After every fix it checks whether the service actually recovered and records success or failure against the signature. Fixes that failed are not proposed first again.
19. **Learns your preferences.** When you deny something with a reason ("don't roll back checkout during business hours"), Happy stores the reason and applies it to future proposals.
20. **Writes the runbook.** Once the same fix has worked twice for a signature, Happy drafts a runbook page and opens it as a pull request, so the knowledge leaves the agent and lands in the team's repo.
21. **Manages its memory deliberately.** Three layers: what happened in this run (summarised as it goes so context never blows up), a long-term incident ledger keyed by signature with counts and outcomes, and a preferences store. Repeats increment a counter instead of adding duplicates. The digest reads only the last seven days.

### The five-minute demo
1. The morning digest lands in Slack, referencing yesterday's incident from memory.
2. A pull request lowers the checkout service's memory limit and is deployed. Pods start dying. Happy's patrol catches it, investigates, and reports "checkout is being OOM-killed; began 3 minutes after PR #4 lowered the limit to 128Mi."
3. Happy proposes a rollback and asks in Slack. The engineer replies "approve". The rollback runs. Happy confirms the service is healthy and opens the postmortem PR.
4. The same failure is triggered again. Happy recognises the signature: "seen before, rollback fixed it in 4 minutes," and proposes it immediately with the evidence. Still asks. Approved, fixed, and now it opens a runbook PR.
5. Happy is asked to delete the namespace. The AWS policy layer blocks it.
6. A look at the CloudWatch trace of the whole run, the incident ledger in memory, and its evaluation scores.

---

## Part 2: Technical design

### Architecture in one paragraph

A Strands agent, deployed on Amazon Bedrock AgentCore Runtime via the AgentCore CLI, is invoked on a schedule by EventBridge Scheduler with a payload like `{"mode": "patrol"}`. It reads the cluster directly through the Kubernetes API, but every write that touches the cluster goes through an AgentCore Gateway (Lambda target) governed by an AgentCore Policy engine. It reads git history from GitHub and talks to humans through Slack, with both tokens held in AgentCore Identity. AgentCore Memory gives it recall across runs. The log specialist uses AgentCore Code Interpreter. CloudWatch records every run as a trace and AgentCore Evaluations scores them.

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

### Feature inventory

Strands (12): agent loop + `@tool`; tool context; interrupts; hooks; Graph multi-agent; agents-as-tools (ledger recall); structured output; session management; summarizing conversation manager; per-node model providers; MCP client; OpenTelemetry tracing. Plus `strands-agents-tools` Code Interpreter tool.

AgentCore (8 of 10): Runtime, Memory, Gateway, Policy, Identity, Code Interpreter, Observability, Evaluations. Skipped: Browser, Payments (no natural use).

Surrounding AWS: EventBridge Scheduler, EC2, Lambda, CloudWatch Transaction Search, AgentCore CLI with CDK.

### The demo cluster
- One `t3.medium` in the default VPC, Ubuntu, k3s installed by user-data with `--tls-san <public ip>`. Security group opens 22 and 6443 (demo-only, documented).
- Namespace `shop` with Deployments `frontend`, `checkout`, `inventory`. Each runs `python:3.12-slim` with a ConfigMap-mounted HTTP server, so nothing is built or pushed.
- Chaos via env vars: `LEAK_MB`, `CRASH_ON_START`, `ERROR_RATE`, `LATENCY_MS`. `checkout` limit 256Mi so OOM is fast.
- Agent and Lambda reach the cluster with a kubeconfig passed as `KUBECONFIG_B64`.

### Strands and AgentCore features, and why each is there

| Feature | Where | Why it is natural |
|---|---|---|
| `@tool` functions | k8s reads, GitHub, Slack | Happy's eyes and voice |
| Hooks (`BeforeToolCallEvent`, `AfterToolCallEvent`) | `guardrails.py` | Audit log; local-only policy fallback |
| Interrupts (`tool_context.interrupt`) | risky write tools | Pause for Slack approval, resume later |
| Session manager + AgentCore Memory | `memory.py` | Pending approval survives restarts; "third time this week" recall |
| Conversation manager (`SummarizingConversationManager`) | `agent.py` | Long investigations stay within context without losing evidence |
| Incident ledger on AgentCore Memory (custom namespaces) | `ledger.py`, `fingerprint.py` | Signature → count, outcomes, last fix; the learning loop |
| Preference strategy | `ledger.py` | Deny reasons become standing preferences |
| Graph multi-agent | `investigate.py` | Three investigators in parallel, one synthesizer |
| Structured output (Pydantic) | `models.py` | Reports are data, so Slack formatting and audit are reliable |
| Per-node models | `investigate.py` | Haiku 4.5 specialists, Sonnet 5 synthesizer |
| MCP client → AgentCore Gateway | `tools/hands.py` | Cluster writes are governed tools, not agent code |
| AgentCore Policy (Cedar) | `agentcore add policy` | Forbid `delete_namespace`; permit `scale` only when `replicas <= 5` |
| AgentCore Identity | `tools/slack.py`, `tools/github.py` | Tokens fetched with `requires_api_key`, never in env |
| AgentCore Code Interpreter | `investigate.py` logs specialist | Pandas over 2,000 log lines in a sandbox |
| AgentCore Runtime + async ack | `main.py` | Long investigations while Scheduler gets an instant ack |
| Observability + Evaluations | CLI `traces`, `add evaluator` | The agent that watches your cluster is itself watched and scored |
| EventBridge Scheduler | `infra/scheduler` | The "runs in the background" part |

### Project layout (AgentCore CLI project, CodeZip build, no Docker)

```
happy/
  README.md  LICENSE(MIT)  AGENTS.md
  agentcore/            agentcore.json, aws-targets.json, .env.local (gitignored), cdk/
  app/happy/
    main.py             BedrockAgentCoreApp entrypoint; payload {"mode": ...}; add_async_task + immediate ack
    pyproject.toml
    config.py           Settings: region, model ids, kubeconfig_b64, slack_channel_id, github_repo, gateway_url, memory_id
    models.py           Finding, ProposedAction, IncidentReport, DigestReport, HandoffNote, Postmortem
    memory.py           build_session_manager(session_id) -> AgentCoreMemorySessionManager | FileSessionManager
                        memory strategies: summary (/summaries/{actorId}/{sessionId}/), userPreference (/preferences/{actorId}/),
                        semantic (/facts/{actorId}/); ledger namespace /incidents/{actorId}/
    fingerprint.py      normalize_line(line) -> str        strips ts, uuids, ips, hex, numbers, pod suffixes
                        top_signatures(lines, k=5) -> list[str]
                        incident_fingerprint(service, reason, lines) -> Fingerprint{id: sha1[:10], service, reason, signatures}
    ledger.py           recall_similar(fp) -> list[IncidentRecord]     exact id match first, then semantic search on signatures
                        remember_incident(fp, report, proposed) -> record_id   (increments count on repeat instead of duplicating)
                        record_outcome(record_id, action, succeeded: bool, minutes_to_recover)
                        remember_preference(text)                       called when a deny reply carries a reason
                        recent_incidents(days=7)                        used by digest
                        (backed by MemoryClient.create_event / retrieve_memories; FileLedger fallback as JSON)
    guardrails.py       AuditHook (AfterToolCallEvent -> audit.jsonl + Slack thread); PolicyHook (local fallback only)
    approvals.py        run_with_approvals(agent, prompt): interrupt loop -> Slack "approve <id>" -> resume
    investigate.py      Graph: triage -> {logs_analyst(+CodeInterpreter), events_analyst, change_analyst} -> synthesizer
    agent.py            build_happy(mode, session_id) -> Agent(model, tools, hooks, session_manager)
    runner.py           run_patrol:  cluster_health -> unhealthy? -> fingerprint -> recall_similar
                                     -> known? propose last successful fix first (with confidence) : run investigate Graph
                                     -> run_with_approvals -> verify recovery (poll health up to 5 min) -> record_outcome
                                     -> remember_incident -> postmortem PR; if fix succeeded twice for this fp -> runbook PR
                        run_digest (uses recent_incidents + audit + GitHub), run_handoff, run_postmortem, run_runbook
    cli.py              local runner: `uv run happy patrol|digest|handoff|investigate`
    tools/
      k8s_read.py       cluster_health, list_unhealthy_pods, get_pod_logs, get_events, describe_deployment, rollout_history
      hands.py          MCPClient to Gateway; wraps rollback/set_image with require_approval() interrupt before the call
      github.py         recent_commits, recent_prs, pr_summary, open_postmortem_pr   (@requires_api_key github-token)
      slack.py          post_message, post_report, wait_for_reply                     (@requires_api_key slack-bot-token)
  lambda/happy-hands/   handler.py: rollout_restart, scale_deployment, rollback_deployment, set_image, delete_namespace
                        tools.json (Gateway tool schema); build.sh (uv pip install --target, zip, create-function)
  policies/happy.cedar  forbid delete_namespace; permit scale when context.input.replicas <= 5; permit the rest
  infra/ec2-k3s/        launch.sh, user-data.sh, fetch-kubeconfig.sh, teardown.sh
  infra/k8s/            namespace.yaml, app-configmap.yaml, frontend.yaml, checkout.yaml, inventory.yaml
  infra/scheduler/      create_schedules.sh (patrol every 10 min, digest daily; universal target InvokeAgentRuntime; Lambda fallback)
  scripts/              chaos.sh {oom|crashloop|badimage|errors|reset}, demo.sh, enable_observability.sh
  docs/                 architecture.md (mermaid) -> architecture.png, devpost.md, blog.md
```

Key CLI commands: `npm i -g @aws/agentcore`; `agentcore create --project-name happy --name happy --language Python --framework Strands --model-provider Bedrock --memory long-term --build CodeZip`; `agentcore add gateway --name HappyHands --authorizer-type NONE --runtimes happy`; `agentcore add gateway-target --name Hands --type lambda-function-arn --lambda-arn ... --tool-schema-file lambda/happy-hands/tools.json --gateway HappyHands`; `agentcore add policy-engine --name HappyPolicy --attach-to-gateways HappyHands --attach-mode ENFORCE`; `agentcore deploy`; `agentcore add policy --name Guardrails --engine HappyPolicy --source policies/happy.cedar`; `agentcore add credential` for the two tokens (values from `agentcore/.env.local`, pasted by the user); `agentcore add evaluator`; `agentcore deploy` again; `agentcore invoke`, `agentcore logs`, `agentcore traces list`.

Verify at build time (not in docs fetched): env var names the CLI injects for gateway URL and memory id (read the generated `main.py`), exact `requires_api_key` import (`bedrock_agentcore.identity.auth`), Scheduler universal-target service id for `InvokeAgentRuntime`.

Models: orchestrator and synthesizer `us.anthropic.claude-sonnet-5`; specialists `us.anthropic.claude-haiku-4-5-20251001-v1:0`.

Secrets: Claude never copies tokens. User pastes `SLACK_BOT_TOKEN` and `GITHUB_TOKEN` into `agentcore/.env.local` and local `.env`.

### Fallbacks (so no single failure sinks the demo)
- Gateway or Policy fails to deploy → `hands.py` calls the k8s write functions directly; `PolicyHook` enforces FORBIDDEN in-process.
- Identity fails → env vars.
- Code Interpreter fails → logs specialist uses plain `get_pod_logs`.
- Scheduler fails → `demo.sh` invokes on a loop.
- Evaluations fails → skip, mention in roadmap.

---

## Part 3: Build order (PDT clock; code freeze 14:30, submit by 15:30)

| When | Step | Done when |
|---|---|---|
| 01:45–02:15 | Slack app config in user's Chrome (scopes `chat:write`, `channels:read`, `channels:history`, `channels:join`; install; create `#happy-oncall`; invite bot). `agentcore create`, MIT license, first push. | `.env.local` filled by user; `git push` works |
| 02:15–03:00 | EC2 + k3s + shop app. Fetch kubeconfig. | 3/3 Running; `chaos.sh oom` shows OOMKilled |
| 03:00–05:00 | `config`, `models`, `tools/k8s_read`, `tools/github`, `tools/slack`, `guardrails`, `approvals`, `agent`, `runner`, `cli`. Bedrock smoke test first. Writes call k8s directly for now. | `happy patrol` restarts a crashloop; rollback asks in Slack and resumes on "approve" |
| 05:00–07:00 | `investigate.py` Graph with Code Interpreter logs specialist; `fingerprint.py`, `ledger.py`, outcome verification; digest, handoff, postmortem and runbook PRs. | Bad-image chaos after a real PR yields a report naming that PR; second OOM is recognised from the ledger; postmortem PR opens |
| 07:00–09:00 | Lambda `happy-hands` + Gateway + Policy engine + Cedar; switch `hands.py` to MCP; Identity credentials; `agentcore deploy`. | `delete_namespace` denied by Policy in Gateway logs; scale to 10 denied, scale to 3 allowed |
| 09:00–10:00 | Transaction Search, `agentcore invoke '{"mode":"patrol"}'`, traces, `add evaluator`, Scheduler. | Ack < 2 s; Slack result; trace and eval scores visible |
| 10:00–11:30 | Full rehearsal of the six demo beats with `demo.sh`, twice. Fix bugs. README drafted in parallel. | Clean run twice |
| 11:30–14:30 | README final, architecture diagram, devpost text, blog draft. User records video. | Clean-clone test passes |
| 14:30–15:30 | Submit on Devpost with Builder ID. | Submitted |

---

## Part 4: How we verify
- Each chaos mode gives the right root cause and tier behaviour: SAFE runs, APPROVAL waits for Slack, FORBIDDEN denied by Policy (Gateway CloudWatch log shows the deny). One audit line per tool call.
- Kill the CLI while waiting for approval, restart with the same session id, reply in Slack, it resumes.
- Learning loop: trigger the same chaos twice. First run goes through the full Graph; second run's report cites the ledger record, count is 2 (not two records), the previously successful action is proposed first, approval is still requested. A deny with a reason appears as a preference on the next proposal. After the second success a runbook PR exists.
- Fingerprints are stable: two OOM runs with different pod names and timestamps produce the same id; an OOM and a CrashLoop on the same service produce different ids.
- Deployed: ack under 2 s, Slack result within ~90 s, trace in CloudWatch GenAI Observability with Graph node spans and a Code Interpreter span, facts in AgentCore Memory, next digest cites them, evaluator scores present.
- Scheduler fires at least once and shows in `agentcore logs`.
- Fresh clone: `agentcore dev` starts; README diagram renders; MIT shows in the About box.
