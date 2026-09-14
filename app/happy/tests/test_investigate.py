"""Tests for investigate.py. No network calls: `make_specialist`/`Agent`/`BedrockModel`
are monkeypatched with in-memory fakes, so the real GraphBuilder/Graph wiring runs
against fake nodes instead of talking to Bedrock."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from strands.agent.agent_result import AgentResult
from strands.telemetry.metrics import EventLoopMetrics

import investigate
from fingerprint import Fingerprint
from guardrails import TIERS
from models import Finding, IncidentReport, ProposedAction


def FakeAgentResult(text="ok", structured_output=None):
    """A real strands AgentResult (Graph's node-execution code does isinstance
    checks against it), just built from plain text instead of a model call."""
    return AgentResult(
        stop_reason="end_turn",
        message={"role": "assistant", "content": [{"text": text}]},
        metrics=EventLoopMetrics(),
        state=None,
        interrupts=None,
        structured_output=structured_output,
    )


class FakeAgent:
    """Duck-types strands.agent.base.AgentBase (a @runtime_checkable Protocol):
    stream_async / invoke_async / __call__. Good enough to be a Graph node without
    ever constructing a real BedrockModel or calling out to AWS."""

    def __init__(self, node_id, text="ok", structured_output=None):
        self.id = node_id
        self.name = node_id
        self._result = FakeAgentResult(text=text, structured_output=structured_output)

    async def stream_async(self, prompt=None, invocation_state=None, **kwargs):
        yield {"result": self._result}

    async def invoke_async(self, prompt=None, **kwargs):
        return self._result

    def __call__(self, prompt=None, **kwargs):
        return self._result


def _context(**overrides):
    context = {
        "service": "checkout",
        "namespace": "shop",
        "time_window": "the last 30 minutes",
        "fingerprint_id": "abc123",
        "signatures": [],
        "unhealthy": [],
    }
    context.update(overrides)
    return context


def _make_specialist_stub(monkeypatch, calls, structured_output=None):
    """Patch investigate.make_specialist so every node is a FakeAgent, and record
    (name, tools) for each call so tests can assert what each node was given."""

    def fake_make_specialist(name, system_prompt, tools, model_id=None, *, structured_output_model=None):
        calls.append({"name": name, "tools": list(tools), "model_id": model_id})
        out = structured_output if (name == "synthesizer" and structured_output is not None) else None
        return FakeAgent(name, text=f"{name} says ok", structured_output=out)

    monkeypatch.setattr(investigate, "make_specialist", fake_make_specialist)


# --- make_specialist ---------------------------------------------------------


def test_make_specialist_uses_bedrock_model_and_settings(monkeypatch):
    fake_settings = SimpleNamespace(model_specialist="haiku-id", model_orchestrator="sonnet-id", aws_region="us-east-1")
    monkeypatch.setattr(investigate, "load_settings", lambda: fake_settings)

    captured = {}

    class FakeBedrockModel:
        def __init__(self, model_id, region_name):
            captured["model_id"] = model_id
            captured["region_name"] = region_name

    fake_agent = MagicMock()
    monkeypatch.setattr(investigate, "BedrockModel", FakeBedrockModel)
    monkeypatch.setattr(investigate, "Agent", fake_agent)

    investigate.make_specialist("triage", "be terse", tools=[])

    assert captured == {"model_id": "haiku-id", "region_name": "us-east-1"}
    fake_agent.assert_called_once()
    _, kwargs = fake_agent.call_args
    assert kwargs["name"] == "triage"
    assert kwargs["system_prompt"] == "be terse"
    assert isinstance(kwargs["model"], FakeBedrockModel)
    assert kwargs["structured_output_model"] is None


def test_make_specialist_respects_explicit_model_id(monkeypatch):
    fake_settings = SimpleNamespace(model_specialist="haiku-id", model_orchestrator="sonnet-id", aws_region="us-east-1")
    monkeypatch.setattr(investigate, "load_settings", lambda: fake_settings)

    captured = {}

    class FakeBedrockModel:
        def __init__(self, model_id, region_name):
            captured["model_id"] = model_id

    monkeypatch.setattr(investigate, "BedrockModel", FakeBedrockModel)
    monkeypatch.setattr(investigate, "Agent", MagicMock())

    investigate.make_specialist("synthesizer", "lead", tools=[], model_id="sonnet-id")

    assert captured["model_id"] == "sonnet-id"


# --- build_investigation_graph -----------------------------------------------


def test_graph_has_five_nodes_six_edges_and_triage_entry_point(monkeypatch):
    calls = []
    _make_specialist_stub(monkeypatch, calls)
    monkeypatch.setattr(investigate, "_code_interpreter_tool", lambda settings: None)
    monkeypatch.setattr(investigate, "load_settings", lambda: SimpleNamespace(
        use_code_interpreter=False, model_orchestrator="sonnet-id", model_specialist="haiku-id", aws_region="us-east-1"
    ))

    graph = investigate.build_investigation_graph(_context())

    assert set(graph.nodes.keys()) == {"triage", "logs_analyst", "events_analyst", "change_analyst", "synthesizer"}
    assert len(graph.nodes) == 5
    assert len(graph.edges) == 6
    assert {node.node_id for node in graph.entry_points} == {"triage"}

    edge_pairs = {(edge.from_node.node_id, edge.to_node.node_id) for edge in graph.edges}
    assert edge_pairs == {
        ("triage", "logs_analyst"),
        ("triage", "events_analyst"),
        ("triage", "change_analyst"),
        ("logs_analyst", "synthesizer"),
        ("events_analyst", "synthesizer"),
        ("change_analyst", "synthesizer"),
    }


def test_synthesizer_built_with_orchestrator_model_and_structured_output(monkeypatch):
    calls = []
    _make_specialist_stub(monkeypatch, calls)
    monkeypatch.setattr(investigate, "_code_interpreter_tool", lambda settings: None)
    monkeypatch.setattr(investigate, "load_settings", lambda: SimpleNamespace(
        use_code_interpreter=False, model_orchestrator="sonnet-id", model_specialist="haiku-id", aws_region="us-east-1"
    ))

    investigate.build_investigation_graph(_context())

    synth_call = next(c for c in calls if c["name"] == "synthesizer")
    assert synth_call["model_id"] == "sonnet-id"


def test_logs_analyst_gets_code_interpreter_tool_when_available(monkeypatch):
    calls = []
    _make_specialist_stub(monkeypatch, calls)
    sentinel_tool = object()
    monkeypatch.setattr(investigate, "_code_interpreter_tool", lambda settings: sentinel_tool)
    monkeypatch.setattr(investigate, "load_settings", lambda: SimpleNamespace(
        use_code_interpreter=True, model_orchestrator="sonnet-id", model_specialist="haiku-id", aws_region="us-east-1"
    ))

    investigate.build_investigation_graph(_context())

    logs_call = next(c for c in calls if c["name"] == "logs_analyst")
    assert sentinel_tool in logs_call["tools"]


def test_logs_analyst_falls_back_without_code_interpreter(monkeypatch):
    calls = []
    _make_specialist_stub(monkeypatch, calls)
    monkeypatch.setattr(investigate, "_code_interpreter_tool", lambda settings: None)
    monkeypatch.setattr(investigate, "load_settings", lambda: SimpleNamespace(
        use_code_interpreter=True, model_orchestrator="sonnet-id", model_specialist="haiku-id", aws_region="us-east-1"
    ))

    investigate.build_investigation_graph(_context())

    logs_call = next(c for c in calls if c["name"] == "logs_analyst")
    assert investigate.get_pod_logs in logs_call["tools"]
    assert len(logs_call["tools"]) == 1


# --- _code_interpreter_tool ---------------------------------------------------


def test_code_interpreter_disabled_by_settings(monkeypatch):
    settings = SimpleNamespace(use_code_interpreter=False, aws_region="us-east-1")
    assert investigate._code_interpreter_tool(settings) is None


def test_code_interpreter_falls_back_on_exception(monkeypatch):
    settings = SimpleNamespace(use_code_interpreter=True, aws_region="us-east-1")

    class BoomInterpreter:
        def __init__(self, region):
            raise RuntimeError("no sandbox credentials")

    monkeypatch.setitem(
        __import__("sys").modules,
        "strands_tools.code_interpreter",
        SimpleNamespace(AgentCoreCodeInterpreter=BoomInterpreter),
    )

    assert investigate._code_interpreter_tool(settings) is None


# --- _sanitize_actions ---------------------------------------------------------


def test_sanitize_actions_drops_unknown_tools_and_fixes_tiers():
    report = IncidentReport(
        service="checkout",
        namespace="shop",
        summary="s",
        root_cause="r",
        confidence=0.8,
        proposed_actions=[
            ProposedAction(tool="rollback_deployment", args={}, tier="SAFE", rationale="wrong tier on purpose"),
            ProposedAction(tool="Hands___rollout_restart", args={}, tier="APPROVAL", rationale="also wrong"),
            ProposedAction(tool="delete_namespace", args={}, tier="SAFE", rationale="model tried to sneak it in"),
            ProposedAction(tool="not_a_real_tool", args={}, tier="SAFE", rationale="garbage"),
            ProposedAction(tool="scale_deployment", args={"replicas": 20}, tier="SAFE", rationale="big scale"),
        ],
    )

    fixed = investigate._sanitize_actions(report)

    tools_and_tiers = {(a.tool, a.tier) for a in fixed.proposed_actions}
    assert tools_and_tiers == {
        ("rollback_deployment", "APPROVAL"),
        ("rollout_restart", "SAFE"),
        ("delete_namespace", "FORBIDDEN"),
        ("scale_deployment", "APPROVAL"),
    }
    assert "not_a_real_tool" not in {a.tool for a in fixed.proposed_actions}
    assert all(a.tool in TIERS for a in fixed.proposed_actions)


# --- investigate() ------------------------------------------------------------


def _fake_graph_result(structured_output=None, text="notes"):
    node_result = SimpleNamespace(result=FakeAgentResult(text=text, structured_output=structured_output))
    return SimpleNamespace(results={"synthesizer": node_result})


def test_investigate_returns_sanitized_structured_output(monkeypatch):
    report = IncidentReport(
        service="checkout",
        namespace="shop",
        summary="checkout is OOMKilled",
        root_cause="memory limit lowered",
        confidence=0.9,
        evidence=[Finding(source="logs_analyst", detail="oom", timestamp="2026-01-01T00:00:00Z")],
        correlated_change="PR #4 lowered checkout memory limit to 128Mi, merged 3 min before first OOMKilled",
        proposed_actions=[ProposedAction(tool="rollback_deployment", args={}, tier="SAFE", rationale="bad change")],
        fingerprint_id=None,
    )

    fake_graph = MagicMock(return_value=_fake_graph_result(structured_output=report))
    monkeypatch.setattr(investigate, "build_investigation_graph", lambda context: fake_graph)

    fp = Fingerprint(id="fp-1", service="checkout", namespace="shop", reason="oom", signatures=["oom killed"])
    result = investigate.investigate("checkout", "shop", fingerprint=fp, unhealthy=[{"pod": "checkout-1"}])

    assert isinstance(result, IncidentReport)
    assert result.fingerprint_id == "fp-1"  # filled in from context, not left None
    assert result.proposed_actions[0].tool == "rollback_deployment"
    assert result.proposed_actions[0].tier == "APPROVAL"  # corrected from the model's wrong "SAFE"
    fake_graph.assert_called_once()
    (task_arg,) = fake_graph.call_args.args
    assert "checkout" in task_arg and "shop" in task_arg and "fp-1" in task_arg


def test_investigate_falls_back_to_extra_structured_output_call(monkeypatch):
    fake_graph = MagicMock(return_value=_fake_graph_result(structured_output=None, text="checkout is unhealthy, no clear cause"))
    monkeypatch.setattr(investigate, "build_investigation_graph", lambda context: fake_graph)

    fallback_report = IncidentReport(
        service="checkout", namespace="shop", summary="fallback summary", root_cause="unclear", confidence=0.3
    )
    fallback_mock = MagicMock(return_value=fallback_report)
    monkeypatch.setattr(investigate, "_fallback_report", fallback_mock)

    result = investigate.investigate("checkout", "shop")

    fallback_mock.assert_called_once()
    assert result.summary == "fallback summary"
    assert result.service == "checkout"
    assert result.namespace == "shop"


def test_fallback_report_calls_agent_with_structured_output_model(monkeypatch):
    fake_settings = SimpleNamespace(model_orchestrator="sonnet-id", aws_region="us-east-1")
    monkeypatch.setattr(investigate, "load_settings", lambda: fake_settings)
    monkeypatch.setattr(investigate, "BedrockModel", lambda model_id, region_name: SimpleNamespace())

    report = IncidentReport(service="checkout", namespace="shop", summary="ok", root_cause="ok", confidence=0.5)
    captured = {}

    class FakeAgentForFallback:
        def __init__(self, model=None, system_prompt=None):
            captured["system_prompt"] = system_prompt

        def __call__(self, text, structured_output_model=None):
            captured["structured_output_model"] = structured_output_model
            return SimpleNamespace(structured_output=report)

    monkeypatch.setattr(investigate, "Agent", FakeAgentForFallback)

    result = investigate._fallback_report(_context(), "some notes")

    assert result is report
    assert captured["structured_output_model"] is IncidentReport


def test_fallback_report_raises_when_no_structured_output(monkeypatch):
    monkeypatch.setattr(investigate, "load_settings", lambda: SimpleNamespace(model_orchestrator="s", aws_region="r"))
    monkeypatch.setattr(investigate, "BedrockModel", lambda model_id, region_name: SimpleNamespace())

    class FakeAgentNoOutput:
        def __init__(self, model=None, system_prompt=None):
            pass

        def __call__(self, text, structured_output_model=None):
            return SimpleNamespace(structured_output=None)

    monkeypatch.setattr(investigate, "Agent", FakeAgentNoOutput)

    with pytest.raises(ValueError):
        investigate._fallback_report(_context(), "notes")


def test_investigate_never_raises_when_graph_construction_fails(monkeypatch):
    def boom(context):
        raise RuntimeError("bedrock unreachable")

    monkeypatch.setattr(investigate, "build_investigation_graph", boom)

    result = investigate.investigate("checkout", "shop")

    assert isinstance(result, IncidentReport)
    assert result.confidence == 0.0
    assert "bedrock unreachable" in result.summary
    assert result.service == "checkout"
    assert result.namespace == "shop"


def test_investigate_never_raises_when_no_synthesizer_result(monkeypatch):
    fake_graph = MagicMock(return_value=SimpleNamespace(results={}))
    monkeypatch.setattr(investigate, "build_investigation_graph", lambda context: fake_graph)

    result = investigate.investigate("checkout", "shop")

    assert isinstance(result, IncidentReport)
    assert result.confidence == 0.0


def test_investigate_end_to_end_with_real_graph_builder(monkeypatch):
    """Exercises the real GraphBuilder/Graph wiring (not mocked away) with fake
    Bedrock-free nodes, proving build_investigation_graph's output is a Graph
    that Graph.__call__ can actually run to completion."""
    report = IncidentReport(
        service="checkout",
        namespace="shop",
        summary="checkout is being OOM-killed",
        root_cause="memory limit too low after a recent change",
        confidence=0.85,
        proposed_actions=[ProposedAction(tool="rollback_deployment", args={}, tier="APPROVAL", rationale="bad change")],
    )
    calls = []
    _make_specialist_stub(monkeypatch, calls, structured_output=report)
    monkeypatch.setattr(investigate, "_code_interpreter_tool", lambda settings: None)
    monkeypatch.setattr(investigate, "load_settings", lambda: SimpleNamespace(
        use_code_interpreter=False, model_orchestrator="sonnet-id", model_specialist="haiku-id", aws_region="us-east-1"
    ))

    result = investigate.investigate("checkout", "shop")

    assert isinstance(result, IncidentReport)
    assert result.root_cause == "memory limit too low after a recent change"
    assert result.proposed_actions[0].tool == "rollback_deployment"
    assert result.proposed_actions[0].tier == "APPROVAL"
    # All five nodes should have been asked to run.
    assert {c["name"] for c in calls} == {"triage", "logs_analyst", "events_analyst", "change_analyst", "synthesizer"}
