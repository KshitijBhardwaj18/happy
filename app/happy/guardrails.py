"""Happy's trust ladder, in code.

Three tiers for anything that changes the cluster:
  SAFE      - Happy just does it and tells you afterwards.
  APPROVAL  - Happy pauses (Strands interrupt) and waits for a human "approve <id>" in Slack.
  FORBIDDEN - never executed. Enforced by AgentCore Policy at the Gateway when deployed,
              and by PolicyHook in-process as a local fallback.

TIERS is the single source of truth; hands.py, investigate.py and the hooks all read it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.types.tools import ToolContext

from config import load_settings

logger = logging.getLogger(__name__)

Tier = Literal["SAFE", "APPROVAL", "FORBIDDEN"]

TIERS: dict[str, Tier] = {
    "rollout_restart": "SAFE",
    "scale_deployment": "SAFE",  # only up to Settings.safe_scale_max; above that -> APPROVAL
    "rollback_deployment": "APPROVAL",
    "set_image": "APPROVAL",
    "delete_namespace": "FORBIDDEN",
}

WRITE_TOOLS = tuple(TIERS)


def tier_for(tool: str, args: dict | None = None) -> Tier | None:
    """Return the tier for a write tool call, or None for read-only tools."""
    name = tool.split("___")[-1]
    tier = TIERS.get(name)
    if tier is None:
        return None
    if name == "scale_deployment" and args and int(args.get("replicas", 0)) > load_settings().safe_scale_max:
        return "APPROVAL"
    return tier


def _tool_name(tool_use: dict) -> str:
    """Strip a Gateway-style `Target___tool` prefix down to the bare tool name."""
    return str(tool_use.get("name", "")).split("___")[-1]


class PolicyHook(HookProvider):
    """Local, in-process fallback for the FORBIDDEN tier of the trust ladder.

    Once Happy runs against the AgentCore Gateway (PR 13/14), Cedar policy attached to
    the Gateway is the real enforcement point -- the call never reaches the Lambda.
    Until then (and as defense in depth afterwards), this hook cancels any call to a
    FORBIDDEN-tier tool before it executes, and logs the tier of every other write-tool
    call so the trust ladder is visible even without reading the audit file.
    """

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._enforce)

    def _enforce(self, event: BeforeToolCallEvent) -> None:
        name = _tool_name(event.tool_use)
        tier = tier_for(name, event.tool_use.get("input"))
        if tier is None:
            return
        if tier == "FORBIDDEN":
            event.cancel_tool = f"Blocked by policy: {name} is FORBIDDEN for Happy"
            logger.warning("PolicyHook blocked %s (FORBIDDEN)", name)
        else:
            logger.info("PolicyHook: %s is tier %s", name, tier)


class AuditHook(HookProvider):
    """Happy's audit trail: one JSONL line per write-tool call, mirrored to Slack.

    Every call to a tool listed in `TIERS` is appended to `Settings.audit_path` as one
    JSON object: `{ts, tool, input, status, tier}`. Read-only tools are not part of the
    trust ladder and are skipped so the audit trail stays about actions, not lookups.
    When `thread_ts` is set (the Slack thread for the incident being handled), the same
    write tools also get one compact line posted to that thread, so a human reading the
    thread sees exactly what Happy did without opening a log file.
    """

    def __init__(self, thread_ts: str | None = None):
        self.thread_ts = thread_ts

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self._audit)

    def _audit(self, event: AfterToolCallEvent) -> None:
        name = _tool_name(event.tool_use)
        tier = tier_for(name, event.tool_use.get("input"))
        if tier is None:
            return

        result = event.result
        if isinstance(result, BaseException):
            status = "error"
        else:
            status = (result or {}).get("status", "success")

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": name,
            "input": event.tool_use.get("input"),
            "status": status,
            "tier": tier,
        }
        self._append(entry)

        if self.thread_ts:
            from tools.slack import post_message

            post_message(f"`{name}` {status} ({tier}) {entry['input']}", thread_ts=self.thread_ts)

    @staticmethod
    def _append(entry: dict[str, Any]) -> None:
        path = load_settings().audit_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")


def require_approval(tool_context: ToolContext, action: str, details: dict) -> tuple[bool, str]:
    """Pause the current tool call for a human "approve"/"deny" decision.

    Raises a Strands interrupt named "approval" carrying `{"action": action, "details":
    details}` as its reason; this stops the agent loop (`AgentResult.stop_reason ==
    "interrupt"`) until `approvals.run_with_approvals` resumes it with a human's reply.
    On resume, `tool_context.interrupt` returns that reply text directly instead of
    raising again. Returns `(True, response)` when the reply starts with "approve"
    (case-insensitive), else `(False, response)`.
    """
    response = tool_context.interrupt("approval", reason={"action": action, "details": details})
    approved = isinstance(response, str) and response.strip().lower().startswith("approve")
    return approved, response
