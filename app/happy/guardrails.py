"""Happy's trust ladder, in code.

Three tiers for anything that changes the cluster:
  SAFE      - Happy just does it and tells you afterwards.
  APPROVAL  - Happy pauses (Strands interrupt) and waits for a human "approve <id>" in Slack.
  FORBIDDEN - never executed. Enforced by AgentCore Policy at the Gateway when deployed,
              and by PolicyHook in-process as a local fallback.

TIERS is the single source of truth; hands.py, investigate.py and the hooks all read it.
"""
from __future__ import annotations

from typing import Literal

from config import load_settings

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
