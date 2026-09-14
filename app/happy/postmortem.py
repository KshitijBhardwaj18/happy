"""Knowledge leaves the agent: postmortems and runbooks.

Once an incident is resolved, `run_postmortem` drafts a postmortem (Sonnet,
falling back to a deterministic template when the model call fails or is
unavailable, e.g. in tests/offline runs) and opens it as a PR via
`tools.github.open_markdown_pr`, then posts the link back to the Slack thread.

Once the same fix has succeeded twice for a fingerprint, `run_runbook` drafts
a runbook page the same way and opens it as its own PR, so the knowledge a
human would otherwise have to re-learn lands in the team's repo instead of
staying trapped in the agent's memory.

Rendering (`render_postmortem` / `render_runbook`) is pure: given a model and
a couple of strings, it returns markdown, with no I/O beyond reading the
(committed, static) template files next to this module.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from strands import Agent
from strands.models import BedrockModel

from config import Settings, load_settings
from ledger import IncidentRecord, Outcome
from models import IncidentReport, Postmortem, Runbook
from tools.github import open_markdown_pr
from tools.slack import post_message

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Best-effort mapping from Hands tool names to the kubectl command a human
# would run by hand, so runbook fix steps stay concrete even before PR 13
# (the real gateway tools) lands. Unknown tools fall back to "n/a".
_KUBECTL_EQUIVALENTS: dict[str, str] = {
    "scale_deployment": "kubectl scale deployment/{deployment} -n {namespace} --replicas={replicas}",
    "restart_deployment": "kubectl rollout restart deployment/{deployment} -n {namespace}",
    "rollout_restart": "kubectl rollout restart deployment/{deployment} -n {namespace}",
    "rollback_deployment": "kubectl rollout undo deployment/{deployment} -n {namespace}",
    "rollout_undo": "kubectl rollout undo deployment/{deployment} -n {namespace}",
    "delete_pod": "kubectl delete pod {pod} -n {namespace}",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(text: str) -> str:
    """Turn a reason string into a lowercase, hyphenated path segment."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "incident"


def _load_template(name: str) -> str:
    return (_TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **mapping: str) -> str:
    """Fill a `{{placeholder}}` template. No braces are ever left in output
    for a key present in `mapping`; unrelated braces (e.g. inside a rendered
    tool-args dict) are left alone since we do plain substring replacement."""
    rendered = template
    for key, value in mapping.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _bulleted(items: list[str], empty: str = "_(none)_") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


# --- timestamps / audit lines ------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_hhmm(dt: datetime | None) -> str:
    return dt.strftime("%H:%M UTC") if dt else "--:-- UTC"


def _audit_time(entry: dict) -> datetime | None:
    return _parse_dt(entry.get("at") or entry.get("ts") or entry.get("timestamp") or entry.get("time"))


def _audit_event_text(entry: dict) -> str:
    tool = entry.get("tool") or entry.get("action") or entry.get("event") or "event"
    args = entry.get("args") or entry.get("params")
    detail = f"{tool}({args})" if args else str(tool)
    result = entry.get("result")
    if result is None and "ok" in entry:
        result = entry.get("ok")
    if result is not None:
        detail += f" -> {result}"
    return detail


def _audit_timeline(audit_lines: list[dict]) -> list[str]:
    """Build "HH:MM UTC — event" timeline bullets from raw audit-log entries."""
    return [f"{_fmt_hhmm(_audit_time(entry))} — {_audit_event_text(entry)}" for entry in audit_lines]


def _last_audit_time(audit_lines: list[dict]) -> datetime | None:
    times = [t for t in (_audit_time(entry) for entry in audit_lines) if t is not None]
    return max(times) if times else None


def _kubectl_equivalent(tool: str, args: dict[str, Any]) -> str:
    template = _KUBECTL_EQUIVALENTS.get(tool)
    if not template:
        return "n/a (see tool args above)"
    try:
        return template.format(**args)
    except (KeyError, IndexError):
        return f"{template} (fill in from args: {args})"


# --- fixes derived from an IncidentRecord's outcomes -------------------------


def _fixes_from_outcomes(outcomes: list[Outcome]) -> list[dict[str, Any]]:
    """Group outcomes by action and rank by success, mirroring
    `Ledger._apply_successful_fixes` but operating on an already-loaded
    record's outcomes directly (no ledger document needed)."""
    groups: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        key = json.dumps(outcome.action, sort_keys=True, default=str)
        group = groups.setdefault(
            key,
            {"action": outcome.action, "success_count": 0, "failure_count": 0, "last_success_at": None},
        )
        if outcome.succeeded:
            group["success_count"] += 1
            if group["last_success_at"] is None or outcome.at > group["last_success_at"]:
                group["last_success_at"] = outcome.at
        else:
            group["failure_count"] += 1

    def sort_key(group: dict[str, Any]) -> tuple[int, int, float]:
        has_success = group["success_count"] > 0
        last_success_ts = group["last_success_at"].timestamp() if has_success else 0.0
        return (0 if has_success else 1, -group["success_count"], -last_success_ts)

    return sorted(groups.values(), key=sort_key)


# --- Postmortem ---------------------------------------------------------------


def _fallback_postmortem(
    report: IncidentReport,
    record: IncidentRecord,
    outcome_action: dict[str, Any],
    succeeded: bool,
    minutes_to_recover: float,
    audit_lines: list[dict],
) -> Postmortem:
    tool = outcome_action.get("tool", "manual intervention")
    timeline = _audit_timeline(audit_lines)
    last_at = _last_audit_time(audit_lines)
    resolved_at = last_at + timedelta(minutes=minutes_to_recover) if last_at else None
    outcome_word = "resolved" if succeeded else "did not resolve"
    timeline.append(
        f"{_fmt_hhmm(resolved_at)} — `{tool}` {outcome_word} the incident "
        f"({minutes_to_recover:.1f} min to recover)"
    )

    action_items = [
        action.rationale
        for action in report.proposed_actions
        if action.tool != tool and action.tier != "SAFE"
    ]
    if succeeded:
        action_items.append(f"Consider promoting `{tool}` to a lower-trust tier for this signature.")
    else:
        action_items.append(f"`{tool}` did not resolve the incident; investigate root cause further.")

    return Postmortem(
        title=f"{report.service} {record.fingerprint.reason}",
        summary=report.summary,
        impact=f"{report.service} in namespace {report.namespace} was affected by "
        f"{record.fingerprint.reason} (confidence {report.confidence:.0%}).",
        timeline=timeline,
        root_cause=report.root_cause,
        resolution=f"Ran `{tool}` which {outcome_word} the incident; "
        f"recovery took {minutes_to_recover:.1f} minutes.",
        action_items=action_items,
    )


def _postmortem_prompt(
    report: IncidentReport,
    record: IncidentRecord,
    outcome_action: dict[str, Any],
    succeeded: bool,
    minutes_to_recover: float,
    audit_lines: list[dict],
) -> str:
    return (
        "Write a postmortem for a resolved production incident. Use the incident report, "
        "the audit log of tool calls (with timestamps), and the outcome below. Build the "
        "`timeline` field as a list of strings in the exact format 'HH:MM UTC — event', "
        "derived strictly from the audit log timestamps (24h UTC), ending with the "
        "resolution event.\n\n"
        f"Incident report:\n{report.model_dump_json(indent=2)}\n\n"
        f"Incident record (fingerprint, history):\n{record.model_dump_json(indent=2)}\n\n"
        f"Audit log (tool calls with timestamps):\n{json.dumps(audit_lines, default=str, indent=2)}\n\n"
        f"Outcome: action={json.dumps(outcome_action, default=str)} succeeded={succeeded} "
        f"minutes_to_recover={minutes_to_recover}\n"
    )


def draft_postmortem(
    report: IncidentReport,
    record: IncidentRecord,
    outcome_action: dict[str, Any],
    succeeded: bool,
    minutes_to_recover: float,
    audit_lines: list[dict],
) -> Postmortem:
    """Draft a Postmortem with Sonnet; fall back to a deterministic one built
    straight from the fields when the model call fails or is unreachable."""
    try:
        settings = load_settings()
        model = BedrockModel(model_id=settings.model_orchestrator, region_name=settings.aws_region)
        agent = Agent(model=model, structured_output_model=Postmortem)
        prompt = _postmortem_prompt(report, record, outcome_action, succeeded, minutes_to_recover, audit_lines)
        result = agent(prompt)
        if result.structured_output is None:
            raise ValueError("agent returned no structured output")
        return result.structured_output
    except Exception:
        logger.exception("draft_postmortem: falling back to deterministic postmortem")
        return _fallback_postmortem(report, record, outcome_action, succeeded, minutes_to_recover, audit_lines)


def render_postmortem(pm: Postmortem, fingerprint_id: str, thread_link: str | None) -> str:
    template = _load_template("postmortem.md")
    return _render(
        template,
        title=pm.title,
        summary=pm.summary,
        impact=pm.impact,
        timeline=_bulleted(pm.timeline),
        root_cause=pm.root_cause,
        resolution=pm.resolution,
        action_items=_bulleted(pm.action_items),
        fingerprint_id=fingerprint_id,
        thread_link=thread_link or "n/a",
    )


# --- Runbook ------------------------------------------------------------------


def _fallback_runbook(record: IncidentRecord, fixes: list[dict[str, Any]]) -> Runbook:
    best = fixes[0]["action"] if fixes else {}
    tool = best.get("tool", "manual intervention")
    args = best.get("args", {}) or {}
    fix_steps = [
        f"Run `{tool}` with args `{args}` — kubectl equivalent: `{_kubectl_equivalent(tool, args)}`.",
        "Watch the deployment reach its desired ready-replica count.",
    ]
    signature_lines = [f"id: {record.fingerprint.id}", *record.fingerprint.signatures]

    return Runbook(
        title=f"{record.fingerprint.service} {record.fingerprint.reason}",
        symptom=record.summary or f"{record.fingerprint.service} experiencing {record.fingerprint.reason}",
        signature="\n".join(signature_lines),
        likely_causes=[record.root_cause] if record.root_cause else [],
        fix_steps=fix_steps,
        verify_steps=[
            "Confirm error rate / health returns to baseline (cluster_health).",
            "Watch for recurrence over the next 30 minutes.",
        ],
        times_seen=record.count,
    )


def _runbook_prompt(record: IncidentRecord, fixes: list[dict[str, Any]]) -> str:
    return (
        "Write a runbook page for a recurring incident signature that has now been fixed "
        "the same way at least twice. `fix_steps` must be numbered steps naming the exact "
        "tool call and its kubectl equivalent.\n\n"
        f"Incident record:\n{record.model_dump_json(indent=2)}\n\n"
        f"Ranked fixes (best first):\n{json.dumps(fixes, default=str, indent=2)}\n"
    )


def draft_runbook(record: IncidentRecord, fixes: list[dict[str, Any]]) -> Runbook:
    """Draft a Runbook with Sonnet; fall back to a deterministic one built
    straight from the record and its ranked fixes when the model call fails."""
    try:
        settings = load_settings()
        model = BedrockModel(model_id=settings.model_orchestrator, region_name=settings.aws_region)
        agent = Agent(model=model, structured_output_model=Runbook)
        result = agent(_runbook_prompt(record, fixes))
        if result.structured_output is None:
            raise ValueError("agent returned no structured output")
        return result.structured_output
    except Exception:
        logger.exception("draft_runbook: falling back to deterministic runbook")
        return _fallback_runbook(record, fixes)


def render_runbook(rb: Runbook, postmortem_paths: list[str]) -> str:
    template = _load_template("runbook.md")
    history_lines = [f"Seen {rb.times_seen} time(s)."]
    if postmortem_paths:
        history_lines.append("Related postmortems:")
        history_lines.extend(f"- {path}" for path in postmortem_paths)
    else:
        history_lines.append("_(no linked postmortems yet)_")

    return _render(
        template,
        heading=rb.title,
        symptom=rb.symptom,
        signature=rb.signature,
        likely_causes=_bulleted(rb.likely_causes),
        fix_steps=_bulleted(rb.fix_steps),
        verify_steps=_bulleted(rb.verify_steps),
        history="\n".join(history_lines),
    )


# --- Slack thread link ---------------------------------------------------------


def _thread_link(thread_ts: str | None, settings: Settings) -> str | None:
    if not thread_ts:
        return None
    return f"https://slack.com/archives/{settings.slack_channel_id}/p{thread_ts.replace('.', '')}"


# --- entry points ---------------------------------------------------------------


def run_postmortem(
    report: IncidentReport,
    record: IncidentRecord,
    outcome_action: dict[str, Any],
    succeeded: bool,
    minutes_to_recover: float,
    thread_ts: str | None,
    audit_lines: list[dict],
) -> dict[str, Any]:
    """Draft and open a postmortem PR for a resolved incident, then post the
    link back to the Slack thread. Returns the `open_markdown_pr` result dict."""
    settings = load_settings()
    pm = draft_postmortem(report, record, outcome_action, succeeded, minutes_to_recover, audit_lines)
    thread_link = _thread_link(thread_ts, settings)
    content = render_postmortem(pm, record.fingerprint.id, thread_link)

    date = _utcnow().strftime("%Y-%m-%d")
    service = record.fingerprint.service
    reason_slug = _slugify(record.fingerprint.reason)
    branch = f"happy/postmortem-{record.id}-{_utcnow().strftime('%Y%m%d')}"
    path = f"docs/postmortems/{date}-{service}-{reason_slug}.md"
    title = f"Postmortem: {service} {record.fingerprint.reason} ({date})"
    body = pm.summary + (f"\n\nSlack thread: {thread_link}" if thread_link else "")

    result = open_markdown_pr(branch=branch, path=path, content=content, title=title, body=body)
    if result.get("ok"):
        post_message(f"\U0001f4dd Postmortem drafted: {result.get('url')}", thread_ts)
    return result


def run_runbook(record: IncidentRecord, postmortem_paths: list[str]) -> dict[str, Any]:
    """Draft and open a runbook PR once the same fix has succeeded at least
    twice for this fingerprint; otherwise a no-op skip result."""
    successes = [outcome for outcome in record.outcomes if outcome.succeeded]
    if len(successes) < 2:
        return {"ok": False, "skipped": "fewer than 2 successful fixes"}

    fixes = _fixes_from_outcomes(record.outcomes)
    rb = draft_runbook(record, fixes)
    content = render_runbook(rb, postmortem_paths)

    service = record.fingerprint.service
    reason_slug = _slugify(record.fingerprint.reason)
    branch = f"happy/runbook-{record.id}"
    path = f"docs/runbooks/{service}-{reason_slug}.md"
    title = f"Runbook: {service} {record.fingerprint.reason}"
    body = rb.symptom

    return open_markdown_pr(branch=branch, path=path, content=content, title=title, body=body)
