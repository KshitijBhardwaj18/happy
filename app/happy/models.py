"""Pydantic data models shared across Happy.

Reports are plain data so Slack formatting, the audit trail, and structured-output
parsing from the agent are all reliable. Every top-level report model implements
`to_slack_text()`, which renders Slack mrkdwn: `*bold*` headers, `•` bullets, kept
compact and readable at phone width (short lines, no wide tables).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

TIER_EMOJI: dict[str, str] = {
    "SAFE": "🟢",
    "APPROVAL": "🟡",
    "FORBIDDEN": "🔴",
}


def _bullets(items: list[str], empty: str = "none") -> str:
    """Render a list of strings as Slack bullet lines, or a single placeholder line."""
    if not items:
        return f"• {empty}"
    return "\n".join(f"• {item}" for item in items)


class Finding(BaseModel):
    """One piece of evidence gathered during an investigation."""

    source: str
    detail: str
    timestamp: str


class ProposedAction(BaseModel):
    """A write action Happy wants to take, tagged with its trust-ladder tier."""

    tool: str
    args: dict
    tier: Literal["SAFE", "APPROVAL", "FORBIDDEN"]
    rationale: str


class IncidentReport(BaseModel):
    """The result of investigating one unhealthy service."""

    service: str
    namespace: str
    summary: str
    root_cause: str
    confidence: float
    evidence: list[Finding] = []
    correlated_change: str | None = None
    proposed_actions: list[ProposedAction] = []
    fingerprint_id: str | None = None

    def to_slack_text(self) -> str:
        lines = [
            f"*Incident: {self.service}* (`{self.namespace}`)",
            "",
            "*Summary*",
            self.summary,
            "",
            "*Root cause*",
            self.root_cause,
            "",
            f"*Confidence:* {self.confidence:.0%}",
        ]

        if self.evidence:
            lines.append("")
            lines.append("*Evidence*")
            for finding in self.evidence[:3]:
                lines.append(f"• [{finding.source}] {finding.detail}")

        if self.correlated_change:
            lines.append("")
            lines.append(f"*Correlated change:* {self.correlated_change}")

        if self.proposed_actions:
            lines.append("")
            lines.append("*Proposed actions*")
            for action in self.proposed_actions:
                emoji = TIER_EMOJI.get(action.tier, "")
                lines.append(f"• {emoji} `{action.tool}` — {action.rationale}")

        return "\n".join(lines)


class DigestReport(BaseModel):
    """The morning digest: what happened overnight, condensed."""

    headline: str
    incidents: list[str] = []
    auto_fixed: list[str] = []
    needs_human: list[str] = []
    open_prs: list[str] = []
    notes_from_memory: list[str] = []

    def to_slack_text(self) -> str:
        return "\n\n".join(
            [
                f"*Morning Digest*\n{self.headline}",
                f"*Incidents overnight*\n{_bullets(self.incidents)}",
                f"*Auto-fixed*\n{_bullets(self.auto_fixed)}",
                f"*Needs human*\n{_bullets(self.needs_human)}",
                f"*Open PRs*\n{_bullets(self.open_prs)}",
                f"*Notes from memory*\n{_bullets(self.notes_from_memory)}",
            ]
        )


class HandoffNote(BaseModel):
    """End-of-shift summary for the next on-call."""

    open_incidents: list[str] = []
    tried: list[str] = []
    watch_list: list[str] = []

    def to_slack_text(self) -> str:
        return "\n\n".join(
            [
                "*Handoff Note*",
                f"*Open incidents*\n{_bullets(self.open_incidents)}",
                f"*Tried*\n{_bullets(self.tried)}",
                f"*Watch list*\n{_bullets(self.watch_list)}",
            ]
        )


class Postmortem(BaseModel):
    """A resolved-incident postmortem, rendered into a markdown PR."""

    title: str
    summary: str
    impact: str
    timeline: list[str] = []
    root_cause: str
    resolution: str
    action_items: list[str] = []

    def to_slack_text(self) -> str:
        return "\n\n".join(
            [
                f"*Postmortem: {self.title}*",
                f"*Summary*\n{self.summary}",
                f"*Impact*\n{self.impact}",
                f"*Timeline*\n{_bullets(self.timeline)}",
                f"*Root cause*\n{self.root_cause}",
                f"*Resolution*\n{self.resolution}",
                f"*Action items*\n{_bullets(self.action_items)}",
            ]
        )


class Runbook(BaseModel):
    """A page of learned knowledge: how a recurring failure was fixed."""

    title: str
    symptom: str
    signature: str
    likely_causes: list[str] = []
    fix_steps: list[str] = []
    verify_steps: list[str] = []
    times_seen: int = 1

    def to_slack_text(self) -> str:
        return "\n\n".join(
            [
                f"*Runbook: {self.title}*",
                f"*Symptom*\n{self.symptom}",
                f"*Signature*\n`{self.signature}`",
                f"*Likely causes*\n{_bullets(self.likely_causes)}",
                f"*Fix steps*\n{_bullets(self.fix_steps)}",
                f"*Verify steps*\n{_bullets(self.verify_steps)}",
                f"*Seen:* {self.times_seen} time(s)",
            ]
        )
