from models import (
    DigestReport,
    Finding,
    HandoffNote,
    IncidentReport,
    Postmortem,
    ProposedAction,
    Runbook,
    TIER_EMOJI,
)


def _incident(**overrides) -> IncidentReport:
    defaults = dict(
        service="checkout",
        namespace="shop",
        summary="checkout is being OOM-killed",
        root_cause="PR #4 lowered the memory limit to 128Mi",
        confidence=0.87,
        evidence=[
            Finding(source="logs", detail="OOMKilled at 03:12", timestamp="2026-09-14T03:12:00Z"),
            Finding(source="events", detail="Back-off restarting failed container", timestamp="2026-09-14T03:12:05Z"),
            Finding(source="events", detail="Killing container checkout", timestamp="2026-09-14T03:12:10Z"),
            Finding(source="logs", detail="memory cgroup out of memory", timestamp="2026-09-14T03:12:11Z"),
        ],
        correlated_change="PR #4: lower checkout memory limit",
        proposed_actions=[
            ProposedAction(tool="rollout_restart", args={}, tier="SAFE", rationale="clear the crash loop"),
            ProposedAction(
                tool="rollback_deployment",
                args={"revision": 3},
                tier="APPROVAL",
                rationale="revert the memory limit change",
            ),
            ProposedAction(
                tool="delete_namespace",
                args={},
                tier="FORBIDDEN",
                rationale="never appropriate here",
            ),
        ],
        fingerprint_id="abc123",
    )
    defaults.update(overrides)
    return IncidentReport(**defaults)


class TestFinding:
    def test_construction(self):
        f = Finding(source="logs", detail="OOMKilled", timestamp="2026-09-14T03:12:00Z")
        assert f.source == "logs"
        assert f.detail == "OOMKilled"
        assert f.timestamp == "2026-09-14T03:12:00Z"


class TestProposedAction:
    def test_tier_literal(self):
        a = ProposedAction(tool="scale_deployment", args={"replicas": 3}, tier="SAFE", rationale="scale up")
        assert a.tier == "SAFE"
        assert a.args == {"replicas": 3}


class TestIncidentReport:
    def test_slack_text_contains_core_fields(self):
        report = _incident()
        text = report.to_slack_text()
        assert "checkout" in text
        assert "shop" in text
        assert report.summary in text
        assert report.root_cause in text

    def test_confidence_rendered_as_percentage(self):
        report = _incident(confidence=0.87)
        text = report.to_slack_text()
        assert "87%" in text
        assert "0.87" not in text

    def test_evidence_capped_at_top_three(self):
        report = _incident()
        text = report.to_slack_text()
        assert text.count("•") >= 3
        # Fourth evidence line must not appear.
        assert "memory cgroup out of memory" not in text
        # First three must appear.
        assert "OOMKilled at 03:12" in text
        assert "Back-off restarting failed container" in text
        assert "Killing container checkout" in text

    def test_correlated_change_shown_when_present(self):
        report = _incident(correlated_change="PR #4: lower checkout memory limit")
        text = report.to_slack_text()
        assert "PR #4: lower checkout memory limit" in text
        assert "Correlated change" in text

    def test_correlated_change_omitted_when_absent(self):
        report = _incident(correlated_change=None)
        text = report.to_slack_text()
        assert "Correlated change" not in text

    def test_proposed_actions_prefixed_by_tier_emoji(self):
        report = _incident()
        text = report.to_slack_text()
        assert f"{TIER_EMOJI['SAFE']} `rollout_restart`" in text
        assert f"{TIER_EMOJI['APPROVAL']} `rollback_deployment`" in text
        assert f"{TIER_EMOJI['FORBIDDEN']} `delete_namespace`" in text

    def test_no_evidence_or_actions_does_not_crash(self):
        report = _incident(evidence=[], proposed_actions=[], correlated_change=None)
        text = report.to_slack_text()
        assert "Evidence" not in text
        assert "Proposed actions" not in text


class TestDigestReport:
    def test_slack_text_lists_all_sections(self):
        report = DigestReport(
            headline="One incident overnight, auto-resolved.",
            incidents=["checkout OOMKilled at 03:12"],
            auto_fixed=["restarted checkout"],
            needs_human=[],
            open_prs=["#4 lower checkout memory limit"],
            notes_from_memory=["checkout has OOMKilled 3 times this week"],
        )
        text = report.to_slack_text()
        assert "Morning Digest" in text
        assert "checkout OOMKilled at 03:12" in text
        assert "restarted checkout" in text
        assert "#4 lower checkout memory limit" in text
        assert "checkout has OOMKilled 3 times this week" in text

    def test_empty_sections_render_placeholder(self):
        report = DigestReport(headline="All quiet.")
        text = report.to_slack_text()
        assert text.count("none") >= 4


class TestHandoffNote:
    def test_slack_text(self):
        note = HandoffNote(
            open_incidents=["checkout still flapping"],
            tried=["restarted twice"],
            watch_list=["inventory memory creeping up"],
        )
        text = note.to_slack_text()
        assert "Handoff Note" in text
        assert "checkout still flapping" in text
        assert "restarted twice" in text
        assert "inventory memory creeping up" in text


class TestPostmortem:
    def test_slack_text(self):
        pm = Postmortem(
            title="checkout OOMKilled",
            summary="checkout pods were killed for exceeding memory limits",
            impact="checkout unavailable for 6 minutes",
            timeline=["03:12 first OOMKilled", "03:16 rollback started", "03:18 recovered"],
            root_cause="PR #4 lowered memory limit to 128Mi",
            resolution="rolled back to previous revision",
            action_items=["revert limit change", "add memory alert"],
        )
        text = pm.to_slack_text()
        assert "Postmortem: checkout OOMKilled" in text
        assert "6 minutes" in text
        assert "03:12 first OOMKilled" in text
        assert "revert limit change" in text


class TestRunbook:
    def test_slack_text(self):
        rb = Runbook(
            title="checkout OOMKilled",
            symptom="checkout pods killed with reason OOMKilled",
            signature="oom|checkout|<n>mi",
            likely_causes=["memory limit too low for load"],
            fix_steps=["rollback_deployment to previous revision"],
            verify_steps=["cluster_health shows checkout ready"],
            times_seen=2,
        )
        text = rb.to_slack_text()
        assert "Runbook: checkout OOMKilled" in text
        assert "oom|checkout|<n>mi" in text
        assert "2 time(s)" in text

    def test_default_times_seen(self):
        rb = Runbook(title="t", symptom="s", signature="sig")
        assert rb.times_seen == 1
