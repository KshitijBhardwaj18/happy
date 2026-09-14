"""Tests for approvals.py: the interrupt/Slack/resume loop, with a fake agent and
monkeypatched Slack functions -- no real network calls."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import approvals


class FakeAgent:
    """Returns each of `results` in order, one per call; records every prompt it saw."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self._results.pop(0)


def _interrupt(id_="abcdef1234", action="rollback_deployment", details=None):
    return SimpleNamespace(id=id_, reason={"action": action, "details": details or {"name": "checkout"}})


def _result(stop_reason, interrupts=None):
    return SimpleNamespace(stop_reason=stop_reason, interrupts=interrupts)


def _settings():
    return SimpleNamespace(approval_timeout_s=600)


# -- resumes on approve ---------------------------------------------------------


def test_run_with_approvals_resumes_after_approve(monkeypatch):
    interrupt_result = _result("interrupt", [_interrupt()])
    final_result = _result("end_turn")
    agent = FakeAgent([interrupt_result, final_result])

    fake_post = MagicMock(return_value="999.000")
    fake_wait = MagicMock(return_value="approve abcdef")
    monkeypatch.setattr(approvals, "post_message", fake_post)
    monkeypatch.setattr(approvals, "wait_for_reply", fake_wait)
    monkeypatch.setattr(approvals, "load_settings", _settings)
    fake_ledger = MagicMock()

    result = approvals.run_with_approvals(agent, "investigate checkout", thread_ts="111.222", ledger=fake_ledger)

    assert result is final_result
    assert agent.calls[0] == "investigate checkout"
    assert agent.calls[1] == [
        {"interruptResponse": {"interruptId": "abcdef1234", "response": "approve abcdef"}}
    ]

    fake_post.assert_called_once()
    posted_text, posted_kwargs = fake_post.call_args
    assert "abcdef" in posted_text[0]
    assert "rollback_deployment" in posted_text[0]
    assert posted_kwargs["thread_ts"] == "111.222"

    fake_wait.assert_called_once()
    assert fake_wait.call_args.kwargs["thread_ts"] == "111.222"

    fake_ledger.remember_preference.assert_not_called()


def test_run_with_approvals_handles_multiple_rounds_of_interrupts(monkeypatch):
    round_one = _result("interrupt", [_interrupt(id_="111111aaaa", action="rollback_deployment")])
    round_two = _result("interrupt", [_interrupt(id_="222222bbbb", action="set_image")])
    final_result = _result("end_turn")
    agent = FakeAgent([round_one, round_two, final_result])

    monkeypatch.setattr(approvals, "post_message", MagicMock(return_value="999.000"))
    monkeypatch.setattr(approvals, "wait_for_reply", MagicMock(return_value="approve 111111"))
    monkeypatch.setattr(approvals, "load_settings", _settings)

    result = approvals.run_with_approvals(agent, "investigate", thread_ts="111.222", ledger=MagicMock())

    assert result is final_result
    assert len(agent.calls) == 3


# -- timeout treated as deny -----------------------------------------------------


def test_run_with_approvals_timeout_is_treated_as_deny(monkeypatch):
    interrupt_result = _result("interrupt", [_interrupt(action="set_image")])
    final_result = _result("end_turn")
    agent = FakeAgent([interrupt_result, final_result])

    monkeypatch.setattr(approvals, "post_message", MagicMock(return_value="999.000"))
    monkeypatch.setattr(approvals, "wait_for_reply", MagicMock(return_value=None))
    monkeypatch.setattr(approvals, "load_settings", _settings)
    fake_ledger = MagicMock()

    approvals.run_with_approvals(agent, "investigate", thread_ts="111.222", ledger=fake_ledger)

    sent = agent.calls[1][0]["interruptResponse"]["response"]
    assert "timed out" in sent
    # "timed out" is only two words -- short deny reasons aren't remembered as preferences.
    fake_ledger.remember_preference.assert_not_called()


# -- deny with a substantial reason becomes a preference -------------------------


def test_run_with_approvals_deny_with_long_reason_remembers_preference(monkeypatch):
    interrupt_result = _result("interrupt", [_interrupt(action="rollback_deployment")])
    final_result = _result("end_turn")
    agent = FakeAgent([interrupt_result, final_result])

    monkeypatch.setattr(approvals, "post_message", MagicMock(return_value="999.000"))
    monkeypatch.setattr(
        approvals,
        "wait_for_reply",
        MagicMock(return_value="deny abcdef don't roll back checkout during business hours"),
    )
    monkeypatch.setattr(approvals, "load_settings", _settings)
    fake_ledger = MagicMock()

    approvals.run_with_approvals(agent, "investigate", thread_ts="111.222", ledger=fake_ledger)

    fake_ledger.remember_preference.assert_called_once()
    text = fake_ledger.remember_preference.call_args.args[0]
    assert "rollback_deployment" in text
    assert "don't roll back checkout during business hours" in text


def test_run_with_approvals_deny_with_short_reason_not_remembered(monkeypatch):
    interrupt_result = _result("interrupt", [_interrupt(action="rollback_deployment")])
    final_result = _result("end_turn")
    agent = FakeAgent([interrupt_result, final_result])

    monkeypatch.setattr(approvals, "post_message", MagicMock(return_value="999.000"))
    monkeypatch.setattr(approvals, "wait_for_reply", MagicMock(return_value="deny abcdef no"))
    monkeypatch.setattr(approvals, "load_settings", _settings)
    fake_ledger = MagicMock()

    approvals.run_with_approvals(agent, "investigate", thread_ts="111.222", ledger=fake_ledger)

    fake_ledger.remember_preference.assert_not_called()


def test_run_with_approvals_uses_get_ledger_when_none_passed(monkeypatch):
    final_result = _result("end_turn")
    agent = FakeAgent([final_result])
    fake_ledger = MagicMock()
    monkeypatch.setattr(approvals, "get_ledger", lambda: fake_ledger)

    result = approvals.run_with_approvals(agent, "patrol", thread_ts=None)

    assert result is final_result
