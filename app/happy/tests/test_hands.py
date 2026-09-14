"""Tests for tools/hands.py: SAFE tools call k8s_write directly, APPROVAL tools gate on
require_approval, delete_namespace always refuses, hands_tools() picks local vs Gateway."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import guardrails as gr
from tools import hands


@pytest.fixture(autouse=True)
def _default_safe_scale_max(monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: SimpleNamespace(safe_scale_max=5))


@pytest.fixture(autouse=True)
def _default_namespace(monkeypatch):
    monkeypatch.setattr(hands, "_default_namespace", lambda: "shop")


# -- SAFE tools call k8s_write directly ---------------------------------------


def test_rollout_restart_calls_k8s_write(monkeypatch):
    fake = MagicMock(return_value={"ok": True, "name": "checkout"})
    monkeypatch.setattr(hands.k8s_write, "rollout_restart", fake)

    result = hands.rollout_restart(name="checkout", namespace="shop")

    fake.assert_called_once_with(name="checkout", namespace="shop")
    assert result == {"ok": True, "name": "checkout"}


def test_rollout_restart_defaults_namespace(monkeypatch):
    fake = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(hands.k8s_write, "rollout_restart", fake)

    hands.rollout_restart(name="checkout")

    fake.assert_called_once_with(name="checkout", namespace="shop")


def test_scale_deployment_below_max_is_safe_no_approval(monkeypatch):
    fake_scale = MagicMock(return_value={"ok": True, "replicas": 3})
    monkeypatch.setattr(hands.k8s_write, "scale_deployment", fake_scale)
    fake_ctx = MagicMock()

    result = hands.scale_deployment(name="checkout", replicas=3, namespace="shop", tool_context=fake_ctx)

    fake_ctx.interrupt.assert_not_called()
    fake_scale.assert_called_once_with(name="checkout", namespace="shop", replicas=3)
    assert result == {"ok": True, "replicas": 3}


# -- scale_deployment above max requires approval -----------------------------


def test_scale_deployment_above_max_asks_for_approval_and_proceeds(monkeypatch):
    fake_scale = MagicMock(return_value={"ok": True, "replicas": 6})
    monkeypatch.setattr(hands.k8s_write, "scale_deployment", fake_scale)
    fake_ctx = MagicMock()
    fake_ctx.interrupt.return_value = "approve ab12cd"

    result = hands.scale_deployment(name="checkout", replicas=6, namespace="shop", tool_context=fake_ctx)

    fake_ctx.interrupt.assert_called_once()
    call_args = fake_ctx.interrupt.call_args
    assert call_args.args[0] == "approval"
    assert call_args.kwargs["reason"]["action"] == "scale_deployment"
    assert call_args.kwargs["reason"]["details"]["replicas"] == 6
    fake_scale.assert_called_once_with(name="checkout", namespace="shop", replicas=6)
    assert result == {"ok": True, "replicas": 6}


def test_scale_deployment_above_max_denied(monkeypatch):
    fake_scale = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(hands.k8s_write, "scale_deployment", fake_scale)
    fake_ctx = MagicMock()
    fake_ctx.interrupt.return_value = "deny ab12cd too risky right now"

    result = hands.scale_deployment(name="checkout", replicas=10, namespace="shop", tool_context=fake_ctx)

    fake_scale.assert_not_called()
    assert result == {"ok": False, "denied": True, "reason": "deny ab12cd too risky right now"}


# -- always-APPROVAL tools -----------------------------------------------------


def test_rollback_deployment_asks_for_approval(monkeypatch):
    fake_rollback = MagicMock(return_value={"ok": True, "rolled_back_to_revision": 2})
    monkeypatch.setattr(hands.k8s_write, "rollback_deployment", fake_rollback)
    fake_ctx = MagicMock()
    fake_ctx.interrupt.return_value = "approve ab12cd"

    result = hands.rollback_deployment(name="checkout", namespace="shop", tool_context=fake_ctx)

    fake_ctx.interrupt.assert_called_once()
    fake_rollback.assert_called_once_with(name="checkout", namespace="shop", revision=None)
    assert result == {"ok": True, "rolled_back_to_revision": 2}


def test_rollback_deployment_denied_never_calls_k8s(monkeypatch):
    fake_rollback = MagicMock()
    monkeypatch.setattr(hands.k8s_write, "rollback_deployment", fake_rollback)
    fake_ctx = MagicMock()
    fake_ctx.interrupt.return_value = "deny ab12cd not now"

    result = hands.rollback_deployment(name="checkout", namespace="shop", tool_context=fake_ctx)

    fake_rollback.assert_not_called()
    assert result["ok"] is False
    assert result["denied"] is True


def test_set_image_asks_for_approval(monkeypatch):
    fake_set_image = MagicMock(return_value={"ok": True, "image": "app:v2"})
    monkeypatch.setattr(hands.k8s_write, "set_image", fake_set_image)
    fake_ctx = MagicMock()
    fake_ctx.interrupt.return_value = "approve ab12cd"

    result = hands.set_image(name="checkout", image="app:v2", namespace="shop", tool_context=fake_ctx)

    fake_ctx.interrupt.assert_called_once()
    fake_set_image.assert_called_once_with(name="checkout", namespace="shop", image="app:v2")
    assert result == {"ok": True, "image": "app:v2"}


# -- delete_namespace is always refused ---------------------------------------


def test_delete_namespace_always_forbidden(monkeypatch):
    fake_delete = MagicMock()
    monkeypatch.setattr(hands.k8s_write, "delete_namespace", fake_delete)

    result = hands.delete_namespace(namespace="shop")

    fake_delete.assert_not_called()
    assert result == {"ok": False, "error": "FORBIDDEN by policy"}


# -- hands_tools() --------------------------------------------------------------


def test_hands_tools_returns_local_tools_by_default(monkeypatch):
    monkeypatch.setattr(hands, "load_settings", lambda: SimpleNamespace(gateway_url=""))

    tools = hands.hands_tools()

    names = {getattr(t, "tool_name", None) for t in tools}
    assert names == {
        "rollout_restart",
        "scale_deployment",
        "rollback_deployment",
        "set_image",
        "delete_namespace",
    }


def test_hands_tools_returns_mcp_client_when_gateway_configured(monkeypatch):
    monkeypatch.setattr(
        hands, "load_settings", lambda: SimpleNamespace(gateway_url="https://gateway.example/mcp")
    )

    tools = hands.hands_tools()

    assert len(tools) == 1
    from strands.tools.mcp.mcp_client import MCPClient

    assert isinstance(tools[0], MCPClient)
