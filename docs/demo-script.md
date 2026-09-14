# Demo script — 5 minutes, six beats

Camera lives in Slack and a terminal running `scripts/chaos.sh`. Consoles (AWS/CloudWatch) get
roughly 30 seconds total, at the very end — this is an Agents for Humans entry, not an infra demo.
The problem, who it's for, and why now all land in the first 40 seconds.

Recording checklist before rolling: `#happy-oncall` open in Slack with no unrelated messages, a
terminal in the repo root with `.env` sourced, `scripts/chaos.sh reset` already run so the cluster
starts healthy, and the previous night's incident already in memory so the digest has something
to reference.

---

## 0:00–0:40 — Cold open: the problem

**Screen:** Black slide, then a single line of text: "On-call is 90% checking, 10% deciding."
Cut to a terminal split three ways — `kubectl get pods -w`, a Slack channel, a laptop clock.

**Voiceover:**
"On-call engineers lose their day to small, repeated checks. What broke. Why. Did the last
deploy cause it. Should I roll back. Who do I tell. Each check is five minutes — together they
eat the day and crowd out the calls that actually need a human. This is Happy: the on-call junior
who never sleeps, and never touches prod without asking. It's built for small teams and solo
developers running their own Kubernetes cluster, with the Strands Agents SDK on Amazon Bedrock
AgentCore. Here's a normal morning with it running."

---

## 0:40–1:20 — Beat 1: the morning digest

**Screen:** Slack, `#happy-oncall`, 9:00am. Happy's digest message is already visible; scroll to
it slowly.

**Voiceover:**
"Every morning at nine, Happy posts a digest: what broke overnight, what it fixed on its own,
what's waiting on a human, and what pull requests are open. This one references yesterday's
incident — 'checkout ran out of memory again' — because it remembers across days, not just
within one conversation."

**On screen (Slack text):** *Morning Digest — Incidents overnight: none. Notes from memory:
checkout OOM'd twice this week, last fixed by rollback in 4 minutes.*

---

## 1:20–2:20 — Beat 2: the patrol catches a real incident

**Screen:** Terminal: `kubectl apply -f infra/k8s` to simulate a PR that lowers checkout's memory
limit, then `scripts/chaos.sh oom`. Cut to `kubectl get pods -w` showing `checkout` pods cycling
into `OOMKilled`. Cut to Slack: a new thread appears within the next patrol cycle.

**Voiceover:**
"A pull request lowers checkout's memory limit and gets deployed. Pods start dying. Happy patrols
every ten minutes — it doesn't wait to be asked. It catches the unhealthy deployment, pulls logs
and events, and reports back."

**On screen (Slack text):** *Incident: checkout (shop) — Summary: checkout is being OOM-killed;
began 3 minutes after PR #4 lowered the memory limit to 128Mi. Root cause: memory limit reduced
below the service's steady-state usage. Confidence: 90%. Correlated change: PR #4.*

---

## 2:20–3:10 — Beat 3: approval, fix, postmortem

**Screen:** Slack thread continues: Happy's proposed action appears (🟡 rollback, APPROVAL tier).
Type `approve <id>` as the engineer. Cut to `kubectl get pods -w` showing checkout recover. Cut
back to Slack: a postmortem PR link appears in the thread.

**Voiceover:**
"Happy proposes a rollback and asks — this is an APPROVAL-tier action, so it pauses and waits.
I reply 'approve.' The rollback runs, Happy confirms checkout is healthy again, and opens a
postmortem pull request in the repo: timeline, root cause, resolution, follow-ups. The knowledge
doesn't stay locked in the agent."

---

## 3:10–3:50 — Beat 4: the second time, it's faster

**Screen:** Terminal: `scripts/chaos.sh oom` again. Cut to Slack: a new thread, this time the
report lands noticeably faster and cites the ledger directly. Approve again. Cut to Slack: a
runbook PR link appears alongside the postmortem link.

**Voiceover:**
"Now I trigger the exact same failure again. Happy recognizes the signature instantly — 'seen
before, rollback fixed it in 4 minutes' — and proposes that fix immediately, with the evidence.
It still asks. I still have to say yes. But this time, because the same fix has now worked twice,
it also opens a runbook page — a page a future on-call engineer can read without ever talking to
the agent at all."

---

## 3:50–4:25 — Beat 5: the hard "no"

**Screen:** Terminal or Slack: ask Happy (via a direct prompt or the CLI) to delete the shop
namespace. Cut to a CloudWatch Logs view of the Gateway, showing a policy deny entry for
`Hands___delete_namespace`. Cut back to `kubectl get ns` showing `shop` still present.

**Voiceover:**
"Now the one I actually want to fail. I ask Happy to delete the namespace. It doesn't reach the
cluster — an AWS policy engine sitting in front of the Gateway blocks the call outright, whether
or not the model wanted to make it. That's not a prompt telling it not to. That's a rule enforced
outside the agent entirely."

---

## 4:25–5:00 — Beat 6: the agent that watches itself

**Screen:** CloudWatch GenAI Observability trace for the whole patrol run — expand to show the
investigation Graph's parallel spans and the Code Interpreter span. Cut to the AgentCore
Evaluations score for the same invocation. Cut to black, title card: repo URL and MIT license.

**Voiceover:**
"Last thing: the agent that watches your cluster is itself watched. Every run is a trace in
CloudWatch — you can see the investigation's specialists running in parallel and the log analysis
in its sandbox. And AgentCore's built-in evaluators score whether it picked the right tools and
gave a useful answer, on every single invocation. Happy is open source, MIT licensed, at
github.com/KshitijBhardwaj18/happy. Thanks for watching."

---

## Notes for the recording

- Keep every Slack screenshot at a size legible on a phone — the judging rubric explicitly credits
  "no dashboard" as the design choice; don't undercut it with tiny text.
- If the AWS console needs more than the allotted 30 seconds to load a trace, pre-open the tabs
  before recording starts and cut to them rather than navigating live.
- Say "approve" and "deny" exactly as Happy expects them (`approve <id>`, `deny <id> <reason>`) so
  the on-screen behavior matches the voiceover without a retake.
