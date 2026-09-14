"""Tests for postmortem.py. The Bedrock agent is always mocked or forced to
fail (via monkeypatching `postmortem.Agent`); no real AWS/Slack/GitHub calls
are made."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import postmortem as pm_mod
from fingerprint import incident_fingerprint
from ledger import IncidentRecord, Outcome
from models import Finding, IncidentReport, Postmortem, ProposedAction, Runbook


# --- fixtures -----------------------------------------------------------------


def _fingerprint(reason: str = "OOMKilled", pod_suffix: str = "7d9f8b6c5d-x2vqp"):
    lines = [
        f"2026-09-14T03:12:01Z ERROR checkout: pod checkout-{pod_suffix} killed, reason OOMKilled",
        "2026-09-14T03:12:02Z WARN checkout: memory usage exceeded limit before kill",
    ]
    return incident_fingerprint("checkout", "shop", reason, lines)


def _report(**overrides) -> IncidentReport:
    defaults = dict(
        service="checkout",
        namespace="shop",
        summary="checkout is being OOM-killed",
        root_cause="PR #4 lowered the memory limit to 128Mi",
        confidence=0.87,
        evidence=[Finding(source="logs", detail="OOMKilled at 03:12", timestamp="2026-09-14T03:12:00Z")],
        correlated_change="PR #4: lower checkout memory limit",
        proposed_actions=[
            ProposedAction(tool="rollout_restart", args={}, tier="SAFE", rationale="clear the crash loop"),
            ProposedAction(
                tool="rollback_deployment",
                args={"revision": 3},
                tier="APPROVAL",
                rationale="revert the memory limit change",
            ),
        ],
        fingerprint_id=_fingerprint().id,
    )
    defaults.update(overrides)
    return IncidentReport(**defaults)


def _record(outcomes: list[Outcome] | None = None) -> IncidentRecord:
    fp = _fingerprint()
    return IncidentRecord(
        id=fp.id,
        fingerprint=fp,
        count=len(outcomes) if outcomes else 1,
        summary="checkout OOMKilled",
        root_cause="PR #4 lowered the memory limit to 128Mi",
        outcomes=outcomes or [],
    )


_AUDIT_LINES = [
    {"at": "2026-09-14T03:12:01Z", "tool": "cluster_health", "result": "unhealthy"},
    {"at": "2026-09-14T03:13:00Z", "tool": "scale_deployment", "args": {"deployment": "checkout", "namespace": "shop", "replicas": 3}, "ok": True},
]

_OUTCOME_ACTION = {"tool": "scale_deployment", "args": {"deployment": "checkout", "namespace": "shop", "replicas": 3}}


class _RaisingAgent:
    """Stand-in for `strands.Agent` that always fails, forcing the deterministic fallback."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("no model access in tests")


class _FakeResult:
    def __init__(self, structured_output):
        self.structured_output = structured_output


class _FakeAgent:
    """Stand-in for `strands.Agent` that returns a canned structured output."""

    def __init__(self, output):
        self._output = output

    def __call__(self, *args, **kwargs):
        return _FakeResult(self._output)


@pytest.fixture(autouse=True)
def _no_real_bedrock(monkeypatch):
    """Belt and suspenders: unless a test explicitly installs its own fake,
    any real Agent/BedrockModel construction fails fast instead of hanging
    on network/credentials."""
    monkeypatch.setattr(pm_mod, "Agent", _RaisingAgent)
    monkeypatch.setattr(pm_mod, "BedrockModel", lambda *a, **k: None)
    yield


# --- render_postmortem ----------------------------------------------------------


def _postmortem() -> Postmortem:
    return Postmortem(
        title="checkout OOMKilled",
        summary="checkout pods were OOM-killed after a memory limit regression.",
        impact="Checkout was unavailable for ~8 minutes.",
        timeline=["03:12 UTC — cluster_health reported unhealthy", "03:20 UTC — scale_deployment resolved it"],
        root_cause="PR #4 lowered the memory limit to 128Mi",
        resolution="Scaled the deployment up while the limit regression is reverted.",
        action_items=["Revert PR #4", "Add a memory-limit regression test"],
    )


def test_render_postmortem_has_all_headings():
    content = pm_mod.render_postmortem(_postmortem(), fingerprint_id="abc123", thread_link="https://slack.com/archives/C1/p1")

    assert content.startswith("# checkout OOMKilled")
    for heading in ["## Summary", "## Impact", "## Timeline", "## Root cause", "## Resolution", "## Action items"]:
        assert heading in content
    assert "03:12 UTC — cluster_health reported unhealthy" in content
    assert "- Revert PR #4" in content
    assert "Drafted by Happy from incident abc123" in content
    assert "https://slack.com/archives/C1/p1" in content


def test_render_postmortem_without_thread_link():
    content = pm_mod.render_postmortem(_postmortem(), fingerprint_id="abc123", thread_link=None)
    assert "Slack thread: n/a" in content


# --- render_runbook ---------------------------------------------------------------


def _runbook() -> Runbook:
    return Runbook(
        title="checkout OOMKilled",
        symptom="checkout pods get OOM-killed under load",
        signature="id: abc123\nerror checkout: pod checkout-<pod> killed, reason oomkilled",
        likely_causes=["Memory limit set too low"],
        fix_steps=["Run `scale_deployment` with args `{'replicas': 3}` — kubectl equivalent: `kubectl scale ...`"],
        verify_steps=["Confirm error rate returns to baseline"],
        times_seen=2,
    )


def test_render_runbook_has_all_headings():
    content = pm_mod.render_runbook(_runbook(), postmortem_paths=["docs/postmortems/2026-09-14-checkout-oomkilled.md"])

    assert content.startswith("# Runbook: checkout OOMKilled")
    for heading in ["## Symptom", "## Signature", "## Likely causes seen so far", "## Fix that worked", "## Verify", "## History"]:
        assert heading in content
    assert "id: abc123" in content
    assert "```text" in content
    assert "Seen 2 time(s)." in content
    assert "docs/postmortems/2026-09-14-checkout-oomkilled.md" in content


def test_render_runbook_with_no_postmortems_yet():
    content = pm_mod.render_runbook(_runbook(), postmortem_paths=[])
    assert "no linked postmortems yet" in content


# --- draft_postmortem ---------------------------------------------------------------


def test_draft_postmortem_falls_back_when_agent_unavailable():
    report = _report()
    record = _record()

    result = pm_mod.draft_postmortem(report, record, _OUTCOME_ACTION, True, 8.5, _AUDIT_LINES)

    assert isinstance(result, Postmortem)
    assert result.summary == report.summary
    assert result.root_cause == report.root_cause
    assert result.timeline  # built from audit lines
    assert "03:12 UTC" in result.timeline[0]
    assert result.action_items


def test_draft_postmortem_falls_back_when_agent_returns_nothing(monkeypatch):
    monkeypatch.setattr(pm_mod, "Agent", lambda *a, **k: _FakeAgent(None))
    report = _report()
    record = _record()

    result = pm_mod.draft_postmortem(report, record, _OUTCOME_ACTION, False, 12.0, _AUDIT_LINES)

    assert isinstance(result, Postmortem)
    assert "did not resolve" in result.resolution


def test_draft_postmortem_uses_agent_structured_output(monkeypatch):
    canned = _postmortem()
    monkeypatch.setattr(pm_mod, "Agent", lambda *a, **k: _FakeAgent(canned))
    report = _report()
    record = _record()

    result = pm_mod.draft_postmortem(report, record, _OUTCOME_ACTION, True, 8.5, _AUDIT_LINES)

    assert result is canned


# --- draft_runbook ---------------------------------------------------------------


def test_draft_runbook_falls_back_when_agent_unavailable():
    outcomes = [
        Outcome(action=_OUTCOME_ACTION, succeeded=True, minutes_to_recover=5.0),
        Outcome(action=_OUTCOME_ACTION, succeeded=True, minutes_to_recover=6.0),
    ]
    record = _record(outcomes)
    fixes = pm_mod._fixes_from_outcomes(record.outcomes)

    result = pm_mod.draft_runbook(record, fixes)

    assert isinstance(result, Runbook)
    assert record.fingerprint.id in result.signature
    assert result.times_seen == record.count
    assert result.fix_steps


def test_draft_runbook_uses_agent_structured_output(monkeypatch):
    canned = _runbook()
    monkeypatch.setattr(pm_mod, "Agent", lambda *a, **k: _FakeAgent(canned))
    record = _record()

    result = pm_mod.draft_runbook(record, [])

    assert result is canned


# --- _fixes_from_outcomes ---------------------------------------------------------------


def test_fixes_from_outcomes_ranks_successes_first():
    good_action = {"tool": "scale_deployment", "args": {"replicas": 3}}
    bad_action = {"tool": "restart_pod", "args": {}}
    outcomes = [
        Outcome(action=bad_action, succeeded=False, minutes_to_recover=0.0),
        Outcome(action=good_action, succeeded=True, minutes_to_recover=5.0),
        Outcome(action=good_action, succeeded=True, minutes_to_recover=4.0),
    ]

    fixes = pm_mod._fixes_from_outcomes(outcomes)

    assert fixes[0]["action"] == good_action
    assert fixes[0]["success_count"] == 2
    assert fixes[-1]["action"] == bad_action
    assert fixes[-1]["failure_count"] == 1


# --- run_postmortem ---------------------------------------------------------------


def test_run_postmortem_opens_pr_and_posts_slack(monkeypatch):
    canned = _postmortem()
    monkeypatch.setattr(pm_mod, "draft_postmortem", lambda *a, **k: canned)
    monkeypatch.setattr(pm_mod, "load_settings", lambda: SimpleNamespace(slack_channel_id="C999"))

    calls = {}

    def _fake_open_pr(branch, path, content, title, body):
        calls["branch"] = branch
        calls["path"] = path
        calls["title"] = title
        calls["body"] = body
        calls["content"] = content
        return {"ok": True, "url": "https://github.com/acme/happy/pull/42", "number": 42}

    posted = {}

    def _fake_post_message(text, thread_ts=None):
        posted["text"] = text
        posted["thread_ts"] = thread_ts
        return "1700000000.000100"

    monkeypatch.setattr(pm_mod, "open_markdown_pr", _fake_open_pr)
    monkeypatch.setattr(pm_mod, "post_message", _fake_post_message)
    monkeypatch.setattr(pm_mod, "_utcnow", lambda: __import__("datetime").datetime(2026, 9, 14, 12, 0, tzinfo=__import__("datetime").timezone.utc))

    record = _record()
    result = pm_mod.run_postmortem(_report(), record, _OUTCOME_ACTION, True, 8.5, "1700000000.000000", _AUDIT_LINES)

    assert result["ok"] is True
    assert calls["branch"] == f"happy/postmortem-{record.id}-20260914"
    assert calls["path"] == f"docs/postmortems/2026-09-14-checkout-{record.fingerprint.reason.lower()}.md"
    assert calls["title"] == f"Postmortem: checkout {record.fingerprint.reason} (2026-09-14)"
    assert "https://slack.com/archives/C999/p1700000000000000" in calls["body"]
    assert posted["thread_ts"] == "1700000000.000000"
    assert "https://github.com/acme/happy/pull/42" in posted["text"]


def test_run_postmortem_skips_slack_post_when_pr_fails(monkeypatch):
    monkeypatch.setattr(pm_mod, "draft_postmortem", lambda *a, **k: _postmortem())
    monkeypatch.setattr(pm_mod, "load_settings", lambda: SimpleNamespace(slack_channel_id="C999"))
    monkeypatch.setattr(pm_mod, "open_markdown_pr", lambda **kwargs: {"ok": False, "error": "boom"})

    posted = {"called": False}
    monkeypatch.setattr(pm_mod, "post_message", lambda *a, **k: posted.update(called=True))

    result = pm_mod.run_postmortem(_report(), _record(), _OUTCOME_ACTION, False, 20.0, None, _AUDIT_LINES)

    assert result["ok"] is False
    assert posted["called"] is False


# --- run_runbook ---------------------------------------------------------------


def test_run_runbook_skips_below_two_successes(monkeypatch):
    called = {"open_pr": False}
    monkeypatch.setattr(pm_mod, "open_markdown_pr", lambda **k: called.update(open_pr=True))

    record = _record([Outcome(action=_OUTCOME_ACTION, succeeded=True, minutes_to_recover=5.0)])
    result = pm_mod.run_runbook(record, postmortem_paths=[])

    assert result == {"ok": False, "skipped": "fewer than 2 successful fixes"}
    assert called["open_pr"] is False


def test_run_runbook_runs_at_two_successes(monkeypatch):
    canned = _runbook()
    monkeypatch.setattr(pm_mod, "draft_runbook", lambda *a, **k: canned)

    calls = {}

    def _fake_open_pr(branch, path, content, title, body):
        calls.update(branch=branch, path=path, title=title, body=body)
        return {"ok": True, "url": "https://github.com/acme/happy/pull/7", "number": 7}

    monkeypatch.setattr(pm_mod, "open_markdown_pr", _fake_open_pr)

    outcomes = [
        Outcome(action=_OUTCOME_ACTION, succeeded=True, minutes_to_recover=5.0),
        Outcome(action=_OUTCOME_ACTION, succeeded=True, minutes_to_recover=6.0),
    ]
    record = _record(outcomes)

    result = pm_mod.run_runbook(record, postmortem_paths=["docs/postmortems/x.md"])

    assert result["ok"] is True
    assert calls["branch"] == f"happy/runbook-{record.id}"
    assert calls["path"] == f"docs/runbooks/checkout-{record.fingerprint.reason.lower()}.md"
    assert calls["title"] == f"Runbook: checkout {record.fingerprint.reason}"
