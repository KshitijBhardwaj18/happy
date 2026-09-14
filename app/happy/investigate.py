"""Investigation graph: three specialists in parallel, one synthesizer.

`investigate(service, namespace, fingerprint, unhealthy)` builds a small Strands
`Graph` (via `GraphBuilder`) shaped `triage -> {logs_analyst, events_analyst,
change_analyst} -> synthesizer`, runs it once with a task string describing the
incident, and returns the synthesizer's structured `IncidentReport`.

Specialists run on `Settings.model_specialist` (Haiku); the synthesizer runs on
`Settings.model_orchestrator` (Sonnet) with `structured_output_model=IncidentReport`.
The logs specialist additionally gets AWS Bedrock AgentCore Code Interpreter's
`code_interpreter` tool when `Settings.use_code_interpreter` is true and the
sandbox can be created; if it cannot, we log and fall back to plain log reading.

`investigate()` never raises: any failure anywhere in the graph, structured-output
parsing, or Code Interpreter setup is caught and turned into a low-confidence
`IncidentReport` describing the failure.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel
from strands import Agent
from strands.models import BedrockModel
from strands.multiagent import GraphBuilder
from strands.multiagent.graph import Graph

from config import load_settings
from fingerprint import Fingerprint
from guardrails import TIERS, tier_for
from models import IncidentReport
from tools.github import pr_summary, recent_commits, recent_prs
from tools.k8s_read import describe_deployment, get_events, get_pod_logs, rollout_history

logger = logging.getLogger(__name__)

_ALLOWED_TOOLS = ", ".join(sorted(TIERS))


def make_specialist(
    name: str,
    system_prompt: str,
    tools: list,
    model_id: str | None = None,
    *,
    structured_output_model: type[BaseModel] | None = None,
) -> Agent:
    """Build a Strands Agent backed by Bedrock, one node's worth of behaviour.

    `model_id` defaults to `Settings.model_specialist` (Haiku); pass
    `Settings.model_orchestrator` for the synthesizer. `structured_output_model`
    is forwarded to `Agent` so the synthesizer can request `IncidentReport` on
    every call without a separate code path.
    """
    settings = load_settings()
    model = BedrockModel(model_id=model_id or settings.model_specialist, region_name=settings.aws_region)
    return Agent(
        name=name,
        system_prompt=system_prompt,
        tools=tools,
        model=model,
        structured_output_model=structured_output_model,
    )


def _code_interpreter_tool(settings) -> object | None:
    """Return the AgentCore Code Interpreter's `code_interpreter` tool, or None.

    Returns None (with a log line) whenever `Settings.use_code_interpreter` is
    false, the package import fails, or sandbox instantiation raises for any
    reason (missing AWS permissions, no network, etc.) -- the logs specialist
    is expected to work fine without it, just less precisely.
    """
    if not settings.use_code_interpreter:
        return None
    try:
        from strands_tools.code_interpreter import AgentCoreCodeInterpreter

        interpreter = AgentCoreCodeInterpreter(region=settings.aws_region)
        return interpreter.code_interpreter
    except Exception as exc:  # noqa: BLE001 - never let sandbox setup sink the investigation
        logger.warning("Code Interpreter unavailable, logs_analyst will use plain log tools: %s", exc)
        return None


def _triage_prompt(context: dict) -> str:
    return (
        "You are the triage lead opening an incident investigation.\n"
        f"Service: `{context['service']}` in namespace `{context['namespace']}`. "
        f"Time window: {context['time_window']}.\n"
        "In your reply: first confirm the service, namespace, and time window in one line. "
        "Then, in short bullets, tell each specialist exactly what to look for:\n"
        "- logs_analyst: which pod(s) to pull current and previous logs for, and what error "
        "pattern or resource symptom (OOM, crash, latency, error rate) to quantify.\n"
        "- events_analyst: what scheduling/restart/image events would explain the symptom, "
        "and to pin down when the first failure happened.\n"
        "- change_analyst: to check infra/k8s/ commits and recent PRs for a change that could "
        "have caused this, and to match its timing against the first failure.\n"
        "Keep it under 10 lines total. This is read by other agents, not a human."
    )


_LOGS_ANALYST_BASE = (
    "You are the logs specialist on an incident investigation team.\n"
    "Pull both the current logs (`get_pod_logs(pod, namespace, previous=False)`) and the "
    "previous run's logs (`previous=True`, if any -- it is normal for this to say there is no "
    "previous run) for the affected pod(s) named in triage's notes or in the unhealthy-pods list "
    "below. Read every line you get back."
)

_LOGS_ANALYST_WITH_CI = (
    "\nYou have a `code_interpreter` tool: use it. Write Python that parses the log lines "
    "(they look like `<ISO timestamp> LEVEL service: message`) into (minute, level) counts, "
    "then report an error-rate-per-minute table (errors / total lines that minute) and the "
    "top 5 most frequent error message signatures (message with numbers/ids masked out). "
    "State the minute the error rate first became non-trivial -- that is the first failure time."
)

_LOGS_ANALYST_NO_CI = (
    "\nNo code sandbox is available this run: scan the lines yourself, report an approximate "
    "error rate per minute from the timestamps you see, and list up to 5 recurring error "
    "message shapes (ignore the varying numbers/ids in each)."
)

_EVENTS_ANALYST_PROMPT = (
    "You are the cluster-events specialist on an incident investigation team.\n"
    "Call `get_events` for the namespace, `describe_deployment` for the affected deployment "
    "(check its image, env vars, and resource limits -- chaos flags like LEAK_MB or "
    "CRASH_ON_START live here), and `rollout_history` to see recent revisions and their images. "
    "State plainly, as your headline finding, when the first failure event happened (timestamp) "
    "and what kind of failure the events point to (OOMKilled, CrashLoopBackOff, ImagePullBackOff, "
    "scheduling failure, etc.)."
)

_CHANGE_ANALYST_PROMPT = (
    "You are the change-correlation specialist on an incident investigation team.\n"
    "Call `recent_commits(path_prefix=\"infra/k8s/\")` and `recent_prs` to see what changed "
    "recently, then `pr_summary` on the pull request that most plausibly touches the affected "
    "service (by files changed and timing). State plainly whether a change landed within 30 "
    "minutes before the first failure time reported elsewhere in this investigation, naming the "
    "PR number and, in one line, what it changed (e.g. \"PR #4 lowered checkout's memory limit "
    "to 128Mi\"). If nothing landed in that window, say so explicitly -- do not force a match."
)

_SYNTHESIZER_PROMPT = (
    "You are the incident lead. Read the triage, logs_analyst, events_analyst, and "
    "change_analyst reports above and write the final IncidentReport.\n"
    "- `fingerprint_id`: copy from the context given in the task if one was provided, else null.\n"
    "- `confidence`: 0-1, how sure you are of the root cause given the evidence.\n"
    "- `evidence`: a Finding per key fact, `source` naming which analyst it came from "
    "(logs_analyst, events_analyst, or change_analyst) and `timestamp` from that finding.\n"
    "- `correlated_change`: if change_analyst found a change within 30 minutes before the "
    "first failure, one line like \"PR #4 lowered checkout memory limit to 128Mi, merged 3 min "
    "before first OOMKilled\"; otherwise null.\n"
    f"- `proposed_actions`: only use tool names from this exact set: {_ALLOWED_TOOLS}. Never "
    "propose delete_namespace. Prefer the smallest fix that addresses the root cause: "
    "rollback_deployment when a specific bad change was identified; rollout_restart for a "
    "transient crash with no identified cause; scale_deployment only if capacity is the issue. "
    "Give each action a `tier` (SAFE, APPROVAL, or FORBIDDEN, matching the trust ladder) and a "
    "one-line `rationale`."
)


def _sanitize_actions(report: IncidentReport) -> IncidentReport:
    """Force `proposed_actions` to only use tools in `guardrails.TIERS`, with the tier
    guardrails.tier_for() actually computes -- never trust the model's own tier label."""
    fixed = []
    for action in report.proposed_actions:
        tool_name = action.tool.split("___")[-1]
        tier = tier_for(tool_name, action.args)
        if tier is None:
            logger.info("Dropping proposed action for unknown/non-write tool: %s", action.tool)
            continue
        fixed.append(action.model_copy(update={"tool": tool_name, "tier": tier}))
    report.proposed_actions = fixed
    return report


def build_investigation_graph(context: dict) -> Graph:
    """Build the (unbuilt-until-called) investigation Graph for one incident.

    `context` carries at least `service`, `namespace`, and `time_window`; it may
    also carry `fingerprint_id` and `unhealthy` (list of unhealthy-pod dicts),
    which are folded into the node system prompts so every specialist starts
    from the same confirmed scope.
    """
    settings = load_settings()

    triage = make_specialist("triage", _triage_prompt(context), tools=[])

    code_interpreter_tool = _code_interpreter_tool(settings)
    logs_tools = [get_pod_logs] + ([code_interpreter_tool] if code_interpreter_tool else [])
    logs_prompt = _LOGS_ANALYST_BASE + (_LOGS_ANALYST_WITH_CI if code_interpreter_tool else _LOGS_ANALYST_NO_CI)
    if context.get("unhealthy"):
        logs_prompt += f"\nUnhealthy pods reported by patrol: {context['unhealthy']}"
    logs_analyst = make_specialist("logs_analyst", logs_prompt, tools=logs_tools)

    events_analyst = make_specialist(
        "events_analyst", _EVENTS_ANALYST_PROMPT, tools=[get_events, describe_deployment, rollout_history]
    )

    change_analyst = make_specialist(
        "change_analyst", _CHANGE_ANALYST_PROMPT, tools=[recent_commits, recent_prs, pr_summary]
    )

    synthesizer = make_specialist(
        "synthesizer",
        _SYNTHESIZER_PROMPT,
        tools=[],
        model_id=settings.model_orchestrator,
        structured_output_model=IncidentReport,
    )

    builder = GraphBuilder()
    builder.add_node(triage, "triage")
    builder.add_node(logs_analyst, "logs_analyst")
    builder.add_node(events_analyst, "events_analyst")
    builder.add_node(change_analyst, "change_analyst")
    builder.add_node(synthesizer, "synthesizer")

    builder.add_edge("triage", "logs_analyst")
    builder.add_edge("triage", "events_analyst")
    builder.add_edge("triage", "change_analyst")
    builder.add_edge("logs_analyst", "synthesizer")
    builder.add_edge("events_analyst", "synthesizer")
    builder.add_edge("change_analyst", "synthesizer")

    builder.set_entry_point("triage")
    return builder.build()


def _build_task(context: dict) -> str:
    lines = [
        f"Investigate why `{context['service']}` in namespace `{context['namespace']}` is unhealthy.",
        f"Time window: {context['time_window']}.",
    ]
    if context.get("fingerprint_id"):
        lines.append(f"Fingerprint id (copy into the final report): {context['fingerprint_id']}")
    if context.get("signatures"):
        lines.append("Known error signatures from the fingerprint: " + "; ".join(context["signatures"]))
    if context.get("unhealthy"):
        lines.append(f"Unhealthy pods: {context['unhealthy']}")
    return "\n".join(lines)


def _fallback_report(context: dict, text: str) -> IncidentReport:
    """One extra Sonnet structured-output call, used only when the synthesizer
    node's own structured_output_model call didn't produce structured output."""
    settings = load_settings()
    model = BedrockModel(model_id=settings.model_orchestrator, region_name=settings.aws_region)
    agent = Agent(
        model=model,
        system_prompt="Extract an IncidentReport from these investigation notes. " + _SYNTHESIZER_PROMPT,
    )
    result = agent(text, structured_output_model=IncidentReport)
    report = result.structured_output
    if report is None:
        raise ValueError("fallback structured-output call produced no structured_output")
    return report


def _empty_report(context: dict, summary: str) -> IncidentReport:
    return IncidentReport(
        service=context["service"],
        namespace=context["namespace"],
        summary=summary,
        root_cause="unknown",
        confidence=0.0,
        fingerprint_id=context.get("fingerprint_id"),
    )


def investigate(
    service: str,
    namespace: str,
    fingerprint: Fingerprint | None = None,
    unhealthy: list[dict] | None = None,
) -> IncidentReport:
    """Run the investigation graph for one unhealthy service and return its report.

    Never raises: any failure (graph construction, model calls, structured-output
    parsing) is caught and turned into an `IncidentReport` with `confidence=0.0`
    whose `summary` describes what went wrong.
    """
    context = {
        "service": service,
        "namespace": namespace,
        "time_window": "the last 30 minutes",
        "fingerprint_id": fingerprint.id if fingerprint else None,
        "signatures": fingerprint.signatures if fingerprint else [],
        "unhealthy": unhealthy or [],
    }
    try:
        graph = build_investigation_graph(context)
        task = _build_task(context)
        result = graph(task)

        node_result = result.results.get("synthesizer")
        if node_result is None:
            logger.warning("Investigation graph produced no synthesizer result for %s/%s", namespace, service)
            return _empty_report(context, "Investigation graph did not reach the synthesizer.")

        agent_result = node_result.result
        report = getattr(agent_result, "structured_output", None)

        if report is None:
            report = _fallback_report(context, str(agent_result))

        if not report.fingerprint_id and context.get("fingerprint_id"):
            report.fingerprint_id = context["fingerprint_id"]
        if not report.service:
            report.service = service
        if not report.namespace:
            report.namespace = namespace

        return _sanitize_actions(report)

    except Exception as exc:  # noqa: BLE001 - investigate() must never raise
        logger.exception("investigate() failed for %s/%s", namespace, service)
        return _empty_report(context, f"Investigation failed: {exc}")
