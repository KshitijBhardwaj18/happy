"""Happy's runners: patrol, digest, and handoff.

`run_patrol` is the core loop, meant to be invoked every 10 minutes: check cluster
health; if everything is healthy, do and say nothing. If a deployment is unhealthy,
fingerprint the failure, check the incident ledger for a fix that has worked before,
otherwise investigate from scratch; propose the fix through Slack approval; verify
recovery; and remember what happened so the next occurrence is recognised. `run_digest`
and `run_handoff` summarize the ledger and audit trail into a report and post it.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from strands import Agent
from strands.models.bedrock import BedrockModel

from agent import SYSTEM_PROMPTS, build_happy
from approvals import run_with_approvals
from config import load_settings
from fingerprint import Fingerprint, incident_fingerprint
from guardrails import tier_for
from ledger import get_ledger
from models import DigestReport, HandoffNote, IncidentReport, ProposedAction
from tools.github import recent_prs
from tools.k8s_read import (
    cluster_health,
    describe_deployment,
    get_events,
    get_pod_logs,
    list_unhealthy_pods,
    rollout_history,
)
from tools.slack import post_message, post_report

logger = logging.getLogger(__name__)

try:  # PR 11: multi-agent investigation Graph. Fall back to a single-agent stub until it lands.
    from investigate import investigate as _graph_investigate
except ImportError:
    _graph_investigate = None


def investigate_stub(service: str, namespace: str, fp: Fingerprint) -> IncidentReport:
    """Investigate an unhealthy service with a single Sonnet call and the read-only
    cluster tools, asked for a structured `IncidentReport`.

    This is a placeholder for `investigate.py`'s multi-agent Graph (PR 11): `run_patrol`
    tries `from investigate import investigate` first and only falls back to this when
    that module doesn't exist yet.
    """
    settings = load_settings()
    agent = Agent(
        model=BedrockModel(model_id=settings.model_orchestrator, region_name=settings.aws_region),
        system_prompt=SYSTEM_PROMPTS["investigate"],
        tools=[
            cluster_health,
            list_unhealthy_pods,
            get_pod_logs,
            get_events,
            describe_deployment,
            rollout_history,
        ],
    )
    prompt = (
        f"Investigate why the deployment `{service}` in namespace `{namespace}` is unhealthy. "
        f"Its failure reason is `{fp.reason}`, and its normalized log signatures are: "
        f"{fp.signatures}. Read logs and events as needed, then produce the IncidentReport."
    )
    result = agent(prompt, structured_output_model=IncidentReport)
    report = result.structured_output
    report.fingerprint_id = fp.id
    return report


def _investigate(service: str, namespace: str, fp: Fingerprint, unhealthy: dict | None = None) -> IncidentReport:
    if _graph_investigate is not None:
        return _graph_investigate(service, namespace, fp, [unhealthy] if unhealthy else None)
    return investigate_stub(service, namespace, fp)


def _first_unhealthy(health: dict) -> dict | None:
    for dep in health.get("deployments", []):
        if not dep.get("healthy", True):
            return dep
    return None


def _first_unhealthy_pod(namespace: str, service: str) -> dict | None:
    pods = list_unhealthy_pods(namespace)
    if not pods:
        return None
    for pod in pods:
        if pod.get("pod", "").startswith(service):
            return pod
    return pods[0]


def _gather_logs(pod: dict | None, namespace: str) -> list[str]:
    """Current + previous log lines for `pod`, best-effort (empty list on any failure)."""
    if pod is None:
        return []
    lines: list[str] = []
    current = get_pod_logs(pod=pod["pod"], namespace=namespace, previous=False)
    if current.get("ok"):
        lines.extend(current.get("lines", []))
    previous = get_pod_logs(pod=pod["pod"], namespace=namespace, previous=True)
    if previous.get("ok"):
        lines.extend(previous.get("lines", []))
    return lines


def _last_success_minutes(record, action: dict) -> float:
    matches = [o for o in record.outcomes if o.succeeded and o.action == action]
    if not matches:
        return 0.0
    return max(matches, key=lambda o: o.at).minutes_to_recover


def _known_fix_report(ledger, service: str, namespace: str, fp: Fingerprint, record) -> IncidentReport | None:
    """Build an IncidentReport proposing a previously-successful fix, or None if this
    record has never had one."""
    fixes = ledger.successful_fixes(record.id)
    fixed = next((f for f in fixes if f.get("success_count", 0) > 0), None)
    if fixed is None:
        return None

    action = fixed["action"]
    tool_name = action.get("tool", "")
    args = action.get("args", {})
    minutes = _last_success_minutes(record, action)

    return IncidentReport(
        service=service,
        namespace=namespace,
        summary=f"Seen {record.count} times before; last fixed by {tool_name} in {minutes:.0f} min",
        root_cause=record.root_cause or record.summary,
        confidence=0.9,
        evidence=[],
        correlated_change=None,
        proposed_actions=[
            ProposedAction(
                tool=tool_name,
                args=args,
                tier=tier_for(tool_name, args) or "SAFE",
                rationale=(
                    f"This exact failure signature was fixed successfully before "
                    f"({fixed['success_count']}x)."
                ),
            )
        ],
        fingerprint_id=fp.id,
    )


def _poll_until_healthy(service: str, namespace: str, timeout_s: int, poll_s: int = 15) -> tuple[bool, float]:
    """Poll `cluster_health` every `poll_s` seconds until `service` is healthy or
    `timeout_s` elapses. Returns `(succeeded, minutes_elapsed)`."""
    start = time.monotonic()
    while True:
        health = cluster_health()
        dep = next(
            (d for d in health.get("deployments", []) if d["name"] == service and d["namespace"] == namespace),
            None,
        )
        elapsed_minutes = (time.monotonic() - start) / 60
        if dep is not None and dep.get("healthy"):
            return True, elapsed_minutes
        if time.monotonic() - start >= timeout_s:
            return False, elapsed_minutes
        time.sleep(poll_s)


def run_patrol(session_id: str | None = None) -> dict:
    """Check the cluster; if it is healthy, do and post nothing. Otherwise, fingerprint
    the first unhealthy deployment, recall a fix that has worked before or investigate
    from scratch, propose it through Slack approval, verify recovery, and remember the
    outcome. Returns `{"status": "all_clear"}` when healthy, else a summary dict.
    """
    settings = load_settings()
    session_id = session_id or f"patrol-{uuid.uuid4().hex[:8]}"

    health = cluster_health()
    unhealthy = _first_unhealthy(health)
    if unhealthy is None:
        return {"status": "all_clear"}

    service = unhealthy["name"]
    namespace = unhealthy["namespace"]
    reason = (unhealthy.get("waiting_reasons") or ["unhealthy"])[0]

    pod = _first_unhealthy_pod(namespace, service)
    log_lines = _gather_logs(pod, namespace)
    fp = incident_fingerprint(service=service, namespace=namespace, reason=reason, log_lines=log_lines)

    ledger = get_ledger()
    recall = ledger.recall_similar(fp)

    report: IncidentReport | None = None
    if recall.records:
        report = _known_fix_report(ledger, service, namespace, fp, recall.records[0])
    if report is None:
        report = _investigate(service, namespace, fp, unhealthy)
        if not report.fingerprint_id:
            report.fingerprint_id = fp.id

    thread_ts = post_report(report)

    for action in report.proposed_actions:
        action_agent = build_happy("patrol", session_id, thread_ts=thread_ts)
        prompt = (
            f"Call the tool `{action.tool}` with exactly these arguments: {action.args}. "
            f"Reason: {action.rationale}. Do not call any other tool for this request. "
            "Once it returns, reply with one short sentence describing what happened."
        )
        run_with_approvals(action_agent, prompt, thread_ts=thread_ts, ledger=ledger)

    succeeded, minutes = _poll_until_healthy(service, namespace, settings.recovery_wait_s)

    action_dict = (
        {"tool": report.proposed_actions[0].tool, "args": report.proposed_actions[0].args}
        if report.proposed_actions
        else {}
    )
    record_id = ledger.remember_incident(fp, report.summary, report.root_cause)
    ledger.record_outcome(record_id, action_dict, succeeded, minutes)

    closing = (
        f"✅ {service} recovered in {minutes:.1f} min."
        if succeeded
        else f"⚠️ {service} still unhealthy after {minutes:.1f} min."
    )
    post_message(closing, thread_ts=thread_ts)

    return {
        "status": "handled",
        "service": service,
        "namespace": namespace,
        "fingerprint_id": fp.id,
        "record_id": record_id,
        "succeeded": succeeded,
        "minutes_to_recover": minutes,
    }


def _read_audit_tail(limit: int = 50) -> list[dict]:
    path = load_settings().audit_path
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    parsed: list[dict] = []
    for line in lines[-limit:]:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed


def run_digest() -> dict:
    """Summarize the last 7 days of incidents, the audit trail, and open pull requests
    into a `DigestReport`, post it to Slack, and return it as a dict."""
    settings = load_settings()
    ledger = get_ledger()
    incidents = ledger.recent_incidents(days=7)
    audit_tail = _read_audit_tail(limit=50)
    prs_result = recent_prs(state="all", hours=24 * 7)
    prs = prs_result.get("prs", []) if isinstance(prs_result, dict) and prs_result.get("ok") else []

    incident_lines = [
        f"{record.fingerprint.service}/{record.fingerprint.reason}: seen {record.count}x, "
        f"last seen {record.last_seen.isoformat()}, summary: {record.summary}"
        for record in incidents
    ]

    agent = Agent(
        model=BedrockModel(model_id=settings.model_orchestrator, region_name=settings.aws_region),
        system_prompt=SYSTEM_PROMPTS["digest"],
    )
    prompt = (
        "Write today's morning digest from this data.\n\n"
        f"Incidents in the last 7 days:\n{incident_lines}\n\n"
        f"Recent audit log entries:\n{audit_tail}\n\n"
        f"Recent pull requests:\n{prs}\n"
    )
    result = agent(prompt, structured_output_model=DigestReport)
    report = result.structured_output
    post_report(report)
    return report.model_dump()


def run_handoff() -> dict:
    """Summarize open incidents and recent actions into a `HandoffNote`, post it to
    Slack, and return it as a dict."""
    settings = load_settings()
    ledger = get_ledger()
    incidents = ledger.recent_incidents(days=1)
    audit_tail = _read_audit_tail(limit=30)

    agent = Agent(
        model=BedrockModel(model_id=settings.model_orchestrator, region_name=settings.aws_region),
        system_prompt=SYSTEM_PROMPTS["handoff"],
    )
    prompt = (
        "Write an end-of-shift handoff note from this data.\n\n"
        f"Open/recent incidents:\n{[record.summary for record in incidents]}\n\n"
        f"Recent actions taken:\n{audit_tail}\n"
    )
    result = agent(prompt, structured_output_model=HandoffNote)
    note = result.structured_output
    post_report(note)
    return note.model_dump()
