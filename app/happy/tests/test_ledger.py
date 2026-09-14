import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fingerprint import incident_fingerprint
from ledger import FileLedger, get_ledger


def _oom_fp(pod_suffix: str = "7d9f8b6c5d-x2vqp") -> "Fingerprint":
    lines = [
        f"2026-09-14T03:12:01Z ERROR checkout: pod checkout-{pod_suffix} killed, reason OOMKilled",
        "2026-09-14T03:12:02Z WARN checkout: memory usage exceeded limit before kill",
    ]
    return incident_fingerprint("checkout", "shop", "OOMKilled", lines)


def _crashloop_fp() -> "Fingerprint":
    lines = [
        "2026-09-14T03:12:01Z FATAL inventory: panic: nil pointer dereference in handler",
        "2026-09-14T03:12:02Z ERROR inventory: CrashLoopBackOff restarting container",
    ]
    return incident_fingerprint("inventory", "shop", "CrashLoopBackOff", lines)


@pytest.fixture
def ledger(tmp_path: Path) -> FileLedger:
    return FileLedger(tmp_path / "ledger.json")


def test_recall_similar_empty_when_nothing_remembered(ledger: FileLedger):
    result = ledger.recall_similar(_oom_fp())
    assert result.records == []
    assert result.related_memories == []


def test_remember_incident_dedupes_and_increments_count(ledger: FileLedger):
    fp1 = _oom_fp("7d9f8b6c5d-x2vqp")
    fp2 = _oom_fp("9a1b2c3d4e-q7wte")  # same signature, different pod name

    record_id_1 = ledger.remember_incident(fp1, summary="checkout OOMKilled", root_cause="memory leak")
    record_id_2 = ledger.remember_incident(fp2, summary="checkout OOMKilled again", root_cause="memory leak")

    assert record_id_1 == record_id_2
    result = ledger.recall_similar(fp1)
    assert len(result.records) == 1
    record = result.records[0]
    assert record.count == 2
    assert record.first_seen <= record.last_seen
    assert record.summary == "checkout OOMKilled again"


def test_oom_and_crashloop_fingerprints_do_not_collide(ledger: FileLedger):
    oom_id = ledger.remember_incident(_oom_fp(), summary="oom", root_cause="leak")
    crash_id = ledger.remember_incident(_crashloop_fp(), summary="crash", root_cause="nil deref")
    assert oom_id != crash_id

    oom_recall = ledger.recall_similar(_oom_fp())
    assert oom_recall.records[0].id == oom_id
    crash_recall = ledger.recall_similar(_crashloop_fp())
    assert crash_recall.records[0].id == crash_id


def test_recall_similar_matches_on_signature_overlap_without_exact_id(ledger: FileLedger):
    fp = _oom_fp()
    record_id = ledger.remember_incident(fp, summary="oom", root_cause="leak")

    # A fingerprint with a different id (different reason string) but enough
    # signature overlap should still surface the existing record.
    near_fp = fp.model_copy(update={"id": "deadbeef00", "reason": "OOMKilled "})
    result = ledger.recall_similar(near_fp)
    assert any(r.id == record_id for r in result.records)


def test_record_outcome_appends_and_successful_fixes_orders_correctly(ledger: FileLedger):
    record_id = ledger.remember_incident(_oom_fp(), summary="oom", root_cause="leak")

    rollback_action = {"tool": "rollback_deployment", "args": {"name": "checkout"}}
    scale_action = {"tool": "scale_deployment", "args": {"replicas": 3}}
    failing_action = {"tool": "restart_pod", "args": {}}

    ledger.record_outcome(record_id, rollback_action, succeeded=True, minutes_to_recover=4.0)
    ledger.record_outcome(record_id, scale_action, succeeded=True, minutes_to_recover=2.0)
    ledger.record_outcome(record_id, scale_action, succeeded=True, minutes_to_recover=2.5)
    ledger.record_outcome(record_id, failing_action, succeeded=False, minutes_to_recover=0.0)

    fixes = ledger.successful_fixes(record_id)

    # scale_action has 2 successes, rollback_action has 1: scale first.
    assert fixes[0]["action"] == scale_action
    assert fixes[0]["success_count"] == 2
    assert fixes[1]["action"] == rollback_action
    assert fixes[1]["success_count"] == 1
    # the failure-only action is last, regardless of recency.
    assert fixes[-1]["action"] == failing_action
    assert fixes[-1]["success_count"] == 0
    assert fixes[-1]["failure_count"] == 1


def test_recent_incidents_filters_by_days(ledger: FileLedger):
    fp = _oom_fp()
    record_id = ledger.remember_incident(fp, summary="oom", root_cause="leak")

    document = ledger._load()
    document.records[record_id].last_seen = datetime.now(timezone.utc) - timedelta(days=30)
    ledger._save(document)

    assert ledger.recent_incidents(days=7) == []
    assert len(ledger.recent_incidents(days=60)) == 1


def test_preferences_persist(ledger: FileLedger):
    ledger.remember_preference("don't roll back checkout during business hours")
    ledger.remember_preference("prefer scale over rollback for inventory")

    prefs = ledger.preferences()
    assert prefs == [
        "don't roll back checkout during business hours",
        "prefer scale over rollback for inventory",
    ]

    # persists across a fresh instance pointed at the same file
    reloaded = FileLedger(ledger.path)
    assert reloaded.preferences() == prefs


def test_get_ledger_returns_file_ledger_when_no_memory(tmp_path, monkeypatch):
    from config import Settings

    settings = Settings(HAPPY_LEDGER_DIR=str(tmp_path), MEMORY_HAPPYMEMORY_ID="")
    result = get_ledger(settings)
    assert isinstance(result, FileLedger)
    assert result.path == tmp_path / "ledger.json"


def test_memory_ledger_smoke_with_mocked_client(monkeypatch):
    """MemoryLedger should round-trip the JSON document and write facts,
    using only methods that exist on the real MemoryClient."""
    fake_module = types.ModuleType("bedrock_agentcore.memory")
    mock_client_cls = MagicMock()
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    fake_module.MemoryClient = mock_client_cls
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.memory", fake_module)

    from ledger import MemoryLedger

    # no ledger snapshot event yet -> empty document
    mock_client.list_events.return_value = []
    mock_client.retrieve_memories.return_value = [
        {"content": {"text": "checkout has OOM'd before; rollback fixed it in 4 minutes"}}
    ]

    memory_ledger = MemoryLedger(memory_id="mem-123", actor_id="sre-team", region="us-east-1")
    mock_client_cls.assert_called_once_with(region_name="us-east-1")

    fp = _oom_fp()
    record_id = memory_ledger.remember_incident(fp, summary="checkout OOMKilled", root_cause="memory leak")
    assert record_id == fp.id

    # remember_incident should persist the JSON snapshot (extraction skipped)
    # and a natural-language fact (extraction enabled).
    assert mock_client.create_event.call_count == 2
    snapshot_call = mock_client.create_event.call_args_list[0]
    assert snapshot_call.kwargs["session_id"] == "ledger"
    assert snapshot_call.kwargs["extraction_mode"] == "SKIP"
    snapshot_text = snapshot_call.kwargs["messages"][0][0]
    assert fp.id in snapshot_text

    fact_call = mock_client.create_event.call_args_list[1]
    assert fact_call.kwargs["session_id"] == "ledger-facts"
    assert "extraction_mode" not in fact_call.kwargs
    assert "OOMKilled" in fact_call.kwargs["messages"][0][0]

    # recall_similar should now find the record by parsing the last snapshot
    # event back out of list_events, and fill related_memories via retrieve_memories.
    mock_client.list_events.return_value = [
        {"payload": [{"conversational": {"content": {"text": snapshot_text}, "role": "ASSISTANT"}}]}
    ]
    result = memory_ledger.recall_similar(fp)
    assert len(result.records) == 1
    assert result.records[0].id == fp.id
    assert result.related_memories == ["checkout has OOM'd before; rollback fixed it in 4 minutes"]
    mock_client.retrieve_memories.assert_called_once()
    assert mock_client.retrieve_memories.call_args.kwargs["namespace"] == "/users/sre-team/facts"

    memory_ledger.remember_preference("don't roll back checkout during business hours")
    pref_call = mock_client.create_event.call_args_list[-1]
    assert pref_call.kwargs["session_id"] == "ledger-facts"
    assert pref_call.kwargs["messages"][0][1] == "USER"
