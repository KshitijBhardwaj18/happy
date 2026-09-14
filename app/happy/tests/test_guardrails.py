"""Tests for guardrails.py: TIERS/tier_for, PolicyHook, AuditHook, require_approval."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

import guardrails as gr


def _settings(tmp_path, safe_scale_max=5):
    return SimpleNamespace(safe_scale_max=safe_scale_max, audit_path=tmp_path / "audit.jsonl")


def _before_event(name: str, input_args: dict):
    return BeforeToolCallEvent(
        agent=SimpleNamespace(),
        selected_tool=None,
        tool_use={"name": name, "input": input_args, "toolUseId": "t1"},
        invocation_state={},
    )


def _after_event(name: str, input_args: dict, result):
    return AfterToolCallEvent(
        agent=SimpleNamespace(),
        selected_tool=None,
        tool_use={"name": name, "input": input_args, "toolUseId": "t1"},
        invocation_state={},
        result=result,
    )


# -- tier_for -----------------------------------------------------------------


def test_tier_for_read_tool_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: _settings(tmp_path))
    assert gr.tier_for("cluster_health") is None


def test_tier_for_scale_above_max_escalates_to_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: _settings(tmp_path, safe_scale_max=5))
    assert gr.tier_for("scale_deployment", {"replicas": 3}) == "SAFE"
    assert gr.tier_for("scale_deployment", {"replicas": 6}) == "APPROVAL"


# -- PolicyHook -----------------------------------------------------------------


def test_policy_hook_cancels_delete_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: _settings(tmp_path))
    hook = gr.PolicyHook()
    event = _before_event("delete_namespace", {"namespace": "shop"})

    hook._enforce(event)

    assert event.cancel_tool
    assert "FORBIDDEN" in event.cancel_tool
    assert "delete_namespace" in event.cancel_tool


def test_policy_hook_allows_safe_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: _settings(tmp_path))
    hook = gr.PolicyHook()
    event = _before_event("rollout_restart", {"name": "checkout", "namespace": "shop"})

    hook._enforce(event)

    assert event.cancel_tool is False


def test_policy_hook_ignores_read_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: _settings(tmp_path))
    hook = gr.PolicyHook()
    event = _before_event("cluster_health", {})

    hook._enforce(event)

    assert event.cancel_tool is False


# -- AuditHook -----------------------------------------------------------------


def test_audit_hook_writes_jsonl_line(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: _settings(tmp_path))
    hook = gr.AuditHook()
    result = {"toolUseId": "t1", "status": "success", "content": [{"text": "ok"}]}
    event = _after_event("rollout_restart", {"name": "checkout", "namespace": "shop"}, result)

    hook._audit(event)

    audit_path = tmp_path / "audit.jsonl"
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "rollout_restart"
    assert entry["tier"] == "SAFE"
    assert entry["status"] == "success"
    assert entry["input"] == {"name": "checkout", "namespace": "shop"}
    assert "ts" in entry


def test_audit_hook_skips_read_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: _settings(tmp_path))
    hook = gr.AuditHook()
    result = {"toolUseId": "t1", "status": "success", "content": []}
    event = _after_event("cluster_health", {}, result)

    hook._audit(event)

    assert not (tmp_path / "audit.jsonl").exists()


def test_audit_hook_posts_to_slack_thread_for_write_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: _settings(tmp_path))
    fake_post_message = MagicMock()
    monkeypatch.setattr("tools.slack.post_message", fake_post_message)

    hook = gr.AuditHook(thread_ts="123.456")
    result = {"toolUseId": "t1", "status": "success", "content": []}
    event = _after_event("rollout_restart", {"name": "checkout", "namespace": "shop"}, result)

    hook._audit(event)

    fake_post_message.assert_called_once()
    args, kwargs = fake_post_message.call_args
    assert kwargs.get("thread_ts") == "123.456"
    assert "rollout_restart" in args[0]


def test_audit_hook_marks_exception_as_error_status(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "load_settings", lambda: _settings(tmp_path))
    hook = gr.AuditHook()
    event = _after_event("rollout_restart", {"name": "checkout"}, RuntimeError("boom"))

    hook._audit(event)

    entry = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert entry["status"] == "error"


# -- require_approval -----------------------------------------------------------


def test_require_approval_true_when_response_starts_with_approve():
    tool_context = MagicMock()
    tool_context.interrupt.return_value = "approve ab12cd"

    approved, response = gr.require_approval(tool_context, "rollback_deployment", {"name": "checkout"})

    assert approved is True
    assert response == "approve ab12cd"
    tool_context.interrupt.assert_called_once_with(
        "approval", reason={"action": "rollback_deployment", "details": {"name": "checkout"}}
    )


def test_require_approval_false_when_response_denies():
    tool_context = MagicMock()
    tool_context.interrupt.return_value = "deny ab12cd not during business hours"

    approved, response = gr.require_approval(tool_context, "set_image", {"name": "checkout"})

    assert approved is False
    assert response == "deny ab12cd not during business hours"
