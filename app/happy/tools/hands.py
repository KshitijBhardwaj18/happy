"""Happy's hands: the five cluster-write tools, gated by the trust ladder.

Each function below mirrors a `tools.k8s_write` function one-for-one -- same name,
same parameter names -- so it lines up with the Gateway's tool schema in
`app/hands/tools.json`. SAFE-tier calls go straight through to `k8s_write`.
`scale_deployment` asks for approval only when the requested replica count is above
`Settings.safe_scale_max` (see `guardrails.tier_for`). The two always-APPROVAL tools
(`rollback_deployment`, `set_image`) ask first, every time. `delete_namespace` is
FORBIDDEN and never touches the cluster, as defense in depth: `guardrails.PolicyHook`
already cancels the call before it gets this far, but this function refuses on its own
too, in case a Gateway/hook path is ever skipped.

`hands_tools()` returns these five local tools, or, once `Settings.gateway_url` is
configured (PR 13/14), a Strands MCP client pointed at the AgentCore Gateway so the
Gateway's Cedar-policy-governed tools replace them entirely.
"""
from __future__ import annotations

from strands import tool
from strands.types.tools import ToolContext

from config import load_settings
from guardrails import require_approval, tier_for
from tools import k8s_write
from tools.k8s_read import _default_namespace


def _ns(namespace: str | None) -> str:
    return namespace or _default_namespace()


@tool(context=True)
def rollout_restart(name: str, namespace: str | None = None, tool_context: ToolContext = None) -> dict:
    """Restart every pod of a Kubernetes deployment via a rolling restart.

    SAFE tier: this fixes stuck or crash-looping pods without changing any
    configuration or version, so Happy may call it without asking a human first.
    """
    return k8s_write.rollout_restart(name=name, namespace=_ns(namespace))


@tool(context=True)
def scale_deployment(
    name: str, replicas: int, namespace: str | None = None, tool_context: ToolContext = None
) -> dict:
    """Set a deployment's replica count.

    SAFE tier up to `Settings.safe_scale_max` (default 5) replicas -- Happy may call it
    without asking. Above that it is APPROVAL tier: Happy pauses and waits for a human
    to reply "approve <id>" in Slack before scaling further. Returns
    `{"ok": False, "denied": True, "reason": ...}` if a human denies the request.
    """
    ns = _ns(namespace)
    if tier_for("scale_deployment", {"replicas": replicas}) == "APPROVAL":
        approved, response = require_approval(
            tool_context,
            action="scale_deployment",
            details={"name": name, "namespace": ns, "replicas": replicas},
        )
        if not approved:
            return {"ok": False, "denied": True, "reason": response}
    return k8s_write.scale_deployment(name=name, namespace=ns, replicas=replicas)


@tool(context=True)
def rollback_deployment(
    name: str,
    namespace: str | None = None,
    revision: int | None = None,
    tool_context: ToolContext = None,
) -> dict:
    """Roll a deployment back to its previous revision (or a given revision).

    APPROVAL tier: this changes what is running in production, so Happy always pauses
    and waits for a human to reply "approve <id>" in Slack before calling it. Returns
    `{"ok": False, "denied": True, "reason": ...}` if a human denies the request.
    """
    ns = _ns(namespace)
    approved, response = require_approval(
        tool_context,
        action="rollback_deployment",
        details={"name": name, "namespace": ns, "revision": revision},
    )
    if not approved:
        return {"ok": False, "denied": True, "reason": response}
    return k8s_write.rollback_deployment(name=name, namespace=ns, revision=revision)


@tool(context=True)
def set_image(
    name: str, image: str, namespace: str | None = None, tool_context: ToolContext = None
) -> dict:
    """Change the container image of a deployment.

    APPROVAL tier: this changes the running version, so Happy always pauses and waits
    for a human to reply "approve <id>" in Slack before calling it. Returns
    `{"ok": False, "denied": True, "reason": ...}` if a human denies the request.
    """
    ns = _ns(namespace)
    approved, response = require_approval(
        tool_context,
        action="set_image",
        details={"name": name, "namespace": ns, "image": image},
    )
    if not approved:
        return {"ok": False, "denied": True, "reason": response}
    return k8s_write.set_image(name=name, namespace=ns, image=image)


@tool(context=True)
def delete_namespace(namespace: str, tool_context: ToolContext = None) -> dict:
    """Delete an entire namespace and everything in it.

    FORBIDDEN tier: Happy must never call this. `guardrails.PolicyHook` already blocks
    this call before it reaches here; this always refuses too, as defense in depth, and
    never touches the cluster.
    """
    return {"ok": False, "error": "FORBIDDEN by policy"}


def hands_tools() -> list:
    """Return Happy's cluster-write tools.

    When `Settings.gateway_url` is configured, returns a single-element list holding a
    Strands `MCPClient` pointed at the AgentCore Gateway, so the Gateway's governed
    tools (Cedar policy enforced server-side) replace these local ones entirely.
    Otherwise returns the five local `@tool` functions above.
    """
    settings = load_settings()
    if settings.gateway_url:
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp.mcp_client import MCPClient

        return [MCPClient(lambda: streamablehttp_client(settings.gateway_url))]
    return [rollout_restart, scale_deployment, rollback_deployment, set_image, delete_namespace]
