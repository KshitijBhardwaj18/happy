"""Tests for runner.py: run_patrol's healthy/known-fix/new-incident paths, and
run_digest/run_handoff building a structured report from the ledger. Every external
tool (k8s, Slack, GitHub, Bedrock via Agent) is monkeypatched -- no real network calls.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import runner
from fingerprint import incident_fingerprint
from ledger import FileLedger
from models import DigestReport, HandoffNote, IncidentReport, ProposedAction

_OOM_LOG_LINES = [
    "2026-09-14T03:12:01Z ERROR checkout: pod checkout-7d9f8b6c5d-x2vqp killed, reason OOMKilled",
    "2026-09-14T03:12:02Z WARN checkout: memory usage exceeded limit before kill",
]


class FakeStructuredAgent:
    """Stands in for `strands.Agent` when only `agent(prompt, structured_output_model=X)`
    matters: returns a canned structured_output regardless of the prompt."""

    def __init__(self, structured_output):
        self._structured_output = structured_output

    def __call__(self, prompt, structured_output_model=None):
        return SimpleNamespace(structured_output=self._structured_output)


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner,
        "load_settings",
        lambda: SimpleNamespace(
            recovery_wait_s=300,
            audit_path=tmp_path / "audit.jsonl",
            model_orchestrator="fake-model",
            aws_region="us-east-1",
        ),
    )


# -- run_patrol: healthy cluster -----------------------------------------------


def test_run_patrol_healthy_returns_all_clear_and_posts_nothing(monkeypatch):
    monkeypatch.setattr(
        runner,
        "cluster_health",
        lambda: {"healthy": True, "deployments": [{"name": "checkout", "namespace": "shop", "healthy": True}]},
    )
    fake_post_report = MagicMock()
    fake_post_message = MagicMock()
    monkeypatch.setattr(runner, "post_report", fake_post_report)
    monkeypatch.setattr(runner, "post_message", fake_post_message)

    result = runner.run_patrol()

    assert result == {"status": "all_clear"}
    fake_post_report.assert_not_called()
    fake_post_message.assert_not_called()


# -- run_patrol: unhealthy, known fingerprint with a stored fix -----------------


def test_run_patrol_known_fingerprint_proposes_stored_fix(monkeypatch, tmp_path: Path):
    ledger = FileLedger(tmp_path / "ledger.json")
    fp = incident_fingerprint("checkout", "shop", "OOMKilled", _OOM_LOG_LINES)
    record_id = ledger.remember_incident(
        fp, summary="checkout OOM-killed", root_cause="memory leak in checkout"
    )
    ledger.record_outcome(
        record_id,
        action={"tool": "rollout_restart", "args": {"name": "checkout", "namespace": "shop"}},
        succeeded=True,
        minutes_to_recover=4.0,
    )

    monkeypatch.setattr(runner, "get_ledger", lambda: ledger)
    monkeypatch.setattr(
        runner,
        "cluster_health",
        lambda: {
            "healthy": False,
            "deployments": [
                {
                    "name": "checkout",
                    "namespace": "shop",
                    "ready": 1,
                    "desired": 2,
                    "restarts": 3,
                    "waiting_reasons": ["OOMKilled"],
                    "healthy": False,
                }
            ],
        },
    )
    monkeypatch.setattr(
        runner,
        "list_unhealthy_pods",
        lambda namespace: [
            {"pod": "checkout-7d9f8b6c5d-x2vqp", "namespace": namespace, "reason": "OOMKilled", "restart_count": 3}
        ],
    )

    def fake_get_pod_logs(pod, namespace=None, previous=False, tail=300):
        if previous:
            return {"ok": False, "error": "no previous run"}
        return {"ok": True, "lines": _OOM_LOG_LINES}

    monkeypatch.setattr(runner, "get_pod_logs", fake_get_pod_logs)

    fake_agent = MagicMock(name="action-agent")
    monkeypatch.setattr(runner, "build_happy", lambda mode, session_id, thread_ts=None: fake_agent)
    fake_run_with_approvals = MagicMock()
    monkeypatch.setattr(runner, "run_with_approvals", fake_run_with_approvals)
    monkeypatch.setattr(runner, "_poll_until_healthy", lambda service, namespace, timeout_s, poll_s=15: (True, 4.0))

    fake_post_report = MagicMock(return_value="111.222")
    fake_post_message = MagicMock()
    monkeypatch.setattr(runner, "post_report", fake_post_report)
    monkeypatch.setattr(runner, "post_message", fake_post_message)

    result = runner.run_patrol(session_id="s1")

    # The report posted to Slack proposes the previously-successful fix, not a fresh
    # investigation.
    posted_report = fake_post_report.call_args.args[0]
    assert isinstance(posted_report, IncidentReport)
    assert posted_report.confidence == 0.9
    assert "Seen 1 times before" in posted_report.summary
    assert "rollout_restart" in posted_report.summary
    assert len(posted_report.proposed_actions) == 1
    assert posted_report.proposed_actions[0].tool == "rollout_restart"
    assert posted_report.proposed_actions[0].tier == "SAFE"

    # The proposed fix was actually run through the approval loop, in the report's thread.
    fake_run_with_approvals.assert_called_once()
    args, kwargs = fake_run_with_approvals.call_args
    assert args[0] is fake_agent
    assert "rollout_restart" in args[1]
    assert kwargs["thread_ts"] == "111.222"

    assert result["status"] == "handled"
    assert result["service"] == "checkout"
    assert result["namespace"] == "shop"
    assert result["fingerprint_id"] == fp.id
    assert result["succeeded"] is True
    assert result["minutes_to_recover"] == 4.0

    # The ledger recognises this as the second occurrence, and now has two outcomes.
    recall = ledger.recall_similar(fp)
    assert recall.records[0].count == 2
    assert len(recall.records[0].outcomes) == 2
    fake_post_message.assert_called_once()
    assert "recovered" in fake_post_message.call_args.args[0].lower()


# -- run_patrol: unhealthy, no known fix -> falls back to investigation --------


def test_run_patrol_unknown_fingerprint_uses_investigation(monkeypatch, tmp_path: Path):
    ledger = FileLedger(tmp_path / "ledger.json")
    monkeypatch.setattr(runner, "get_ledger", lambda: ledger)
    monkeypatch.setattr(
        runner,
        "cluster_health",
        lambda: {
            "healthy": False,
            "deployments": [
                {
                    "name": "inventory",
                    "namespace": "shop",
                    "ready": 0,
                    "desired": 2,
                    "restarts": 5,
                    "waiting_reasons": ["CrashLoopBackOff"],
                    "healthy": False,
                }
            ],
        },
    )
    monkeypatch.setattr(runner, "list_unhealthy_pods", lambda namespace: [])
    monkeypatch.setattr(runner, "get_pod_logs", lambda **kwargs: {"ok": False, "error": "no pod"})

    fresh_report = IncidentReport(
        service="inventory",
        namespace="shop",
        summary="inventory is crash-looping on startup",
        root_cause="nil pointer dereference",
        confidence=0.6,
        proposed_actions=[
            ProposedAction(
                tool="rollout_restart",
                args={"name": "inventory", "namespace": "shop"},
                tier="SAFE",
                rationale="restart clears a bad in-memory state",
            )
        ],
    )
    monkeypatch.setattr(runner, "_investigate", lambda service, namespace, fp, unhealthy=None: fresh_report)
    monkeypatch.setattr(runner, "build_happy", lambda mode, session_id, thread_ts=None: MagicMock())
    monkeypatch.setattr(runner, "run_with_approvals", MagicMock())
    monkeypatch.setattr(runner, "_poll_until_healthy", lambda service, namespace, timeout_s, poll_s=15: (True, 1.5))
    monkeypatch.setattr(runner, "post_report", MagicMock(return_value="222.333"))
    monkeypatch.setattr(runner, "post_message", MagicMock())

    result = runner.run_patrol()

    assert result["status"] == "handled"
    assert result["service"] == "inventory"
    assert result["succeeded"] is True

    recall = ledger.recall_similar(
        incident_fingerprint("inventory", "shop", "CrashLoopBackOff", [])
    )
    assert len(recall.records) == 1
    assert recall.records[0].count == 1


# -- run_digest / run_handoff ---------------------------------------------------


def test_run_digest_builds_report_from_ledger_and_posts(monkeypatch, tmp_path: Path):
    ledger = FileLedger(tmp_path / "ledger.json")
    fp = incident_fingerprint("checkout", "shop", "OOMKilled", _OOM_LOG_LINES)
    ledger.remember_incident(fp, summary="checkout OOM-killed", root_cause="memory leak")
    monkeypatch.setattr(runner, "get_ledger", lambda: ledger)
    monkeypatch.setattr(runner, "recent_prs", lambda **kwargs: {"ok": True, "prs": []})

    fake_report = DigestReport(headline="Quiet night", incidents=["checkout OOM x1"])
    monkeypatch.setattr(runner, "Agent", lambda *a, **k: FakeStructuredAgent(fake_report))
    fake_post_report = MagicMock()
    monkeypatch.setattr(runner, "post_report", fake_post_report)

    result = runner.run_digest()

    assert result["headline"] == "Quiet night"
    fake_post_report.assert_called_once_with(fake_report)


def test_run_handoff_builds_note_from_ledger_and_posts(monkeypatch, tmp_path: Path):
    ledger = FileLedger(tmp_path / "ledger.json")
    monkeypatch.setattr(runner, "get_ledger", lambda: ledger)

    fake_note = HandoffNote(open_incidents=["none"], tried=[], watch_list=["checkout memory"])
    monkeypatch.setattr(runner, "Agent", lambda *a, **k: FakeStructuredAgent(fake_note))
    fake_post_report = MagicMock()
    monkeypatch.setattr(runner, "post_report", fake_post_report)

    result = runner.run_handoff()

    assert result["watch_list"] == ["checkout memory"]
    fake_post_report.assert_called_once_with(fake_note)
