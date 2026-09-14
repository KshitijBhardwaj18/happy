"""Tests for tools/slack.py. WebClient is always mocked; no real Slack calls."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from tools import slack as sl


def _fake_settings(
    slack_bot_token="xoxb-fake",
    slack_channel_id="C123",
    approval_timeout_s=600,
):
    has_slack = bool(slack_bot_token and slack_channel_id)
    return SimpleNamespace(
        slack_bot_token=slack_bot_token,
        slack_channel_id=slack_channel_id,
        approval_timeout_s=approval_timeout_s,
        has_slack=has_slack,
    )


@pytest.fixture(autouse=True)
def _reset_module_cache():
    sl._reset_cache()
    yield
    sl._reset_cache()


def _slack_api_error(message="boom"):
    response = MagicMock()
    response.get.return_value = None
    return SlackApiError(message=message, response=response)


# --- post_message -----------------------------------------------------------


def test_post_message_noop_when_slack_not_configured(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings(slack_bot_token=""))
    fake_client = MagicMock()
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    result = sl.post_message("hello")

    assert result is None
    fake_client.chat_postMessage.assert_not_called()


def test_post_message_posts_and_returns_ts(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.chat_postMessage.return_value = {"ok": True, "ts": "111.222"}
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    result = sl.post_message("incident detected", thread_ts="000.111")

    assert result == "111.222"
    fake_client.chat_postMessage.assert_called_once_with(
        channel="C123", text="incident detected", thread_ts="000.111"
    )


def test_post_message_returns_none_on_slack_api_error(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.chat_postMessage.side_effect = _slack_api_error()
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    result = sl.post_message("hello")
    assert result is None


# --- post_report -----------------------------------------------------------


def test_post_report_uses_to_slack_text_when_available(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.chat_postMessage.return_value = {"ts": "1.1"}
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    report = SimpleNamespace(to_slack_text=lambda: "*Incident*: checkout OOM")
    result = sl.post_report(report, thread_ts="9.9")

    assert result == "1.1"
    fake_client.chat_postMessage.assert_called_once_with(
        channel="C123", text="*Incident*: checkout OOM", thread_ts="9.9"
    )


def test_post_report_falls_back_to_str_without_to_slack_text(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.chat_postMessage.return_value = {"ts": "1.1"}
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    result = sl.post_report({"headline": "all clear"})

    assert result == "1.1"
    args, kwargs = fake_client.chat_postMessage.call_args
    assert kwargs["text"] == str({"headline": "all clear"})


def test_post_report_noop_when_slack_not_configured(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings(slack_bot_token=""))
    fake_client = MagicMock()
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    result = sl.post_report(SimpleNamespace(to_slack_text=lambda: "text"))
    assert result is None
    fake_client.chat_postMessage.assert_not_called()


# --- wait_for_reply -----------------------------------------------------------


def test_wait_for_reply_noop_when_slack_not_configured(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings(slack_bot_token=""))
    fake_client = MagicMock()
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    result = sl.wait_for_reply(pattern="approve", after_ts="1.0")
    assert result is None
    fake_client.conversations_history.assert_not_called()


def test_wait_for_reply_finds_matching_human_message_on_first_poll(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.conversations_history.return_value = {
        "messages": [
            {"ts": "5.0", "text": "Approve abc123", "user": "U1"},
        ]
    }
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)
    sleep_mock = MagicMock()
    monkeypatch.setattr(sl.time, "sleep", sleep_mock)

    result = sl.wait_for_reply(pattern=r"^approve", after_ts="1.0", timeout_s=30, poll_s=5)

    assert result == "Approve abc123"
    sleep_mock.assert_not_called()
    fake_client.conversations_history.assert_called_once_with(channel="C123", oldest="1.0")


def test_wait_for_reply_uses_conversations_replies_when_thread_ts_given(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.conversations_replies.return_value = {
        "messages": [{"ts": "5.0", "text": "deny not now", "user": "U1"}]
    }
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    result = sl.wait_for_reply(pattern=r"deny", after_ts="1.0", thread_ts="9.0")

    assert result == "deny not now"
    fake_client.conversations_replies.assert_called_once_with(channel="C123", ts="9.0", oldest="1.0")
    fake_client.conversations_history.assert_not_called()


def test_wait_for_reply_ignores_bot_and_subtype_messages(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.conversations_history.side_effect = [
        {
            "messages": [
                {"ts": "2.0", "text": "approve please", "bot_id": "B1"},
                {"ts": "3.0", "text": "approve please", "subtype": "message_changed"},
            ]
        },
        {"messages": [{"ts": "4.0", "text": "approve please", "user": "U1"}]},
    ]
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)
    monkeypatch.setattr(sl.time, "sleep", MagicMock())

    result = sl.wait_for_reply(pattern="approve", after_ts="1.0", timeout_s=30, poll_s=1)

    assert result == "approve please"
    assert fake_client.conversations_history.call_count == 2


def test_wait_for_reply_ignores_case_and_matches_substring(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.conversations_history.return_value = {
        "messages": [{"ts": "2.0", "text": "APPROVE 42", "user": "U1"}]
    }
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    result = sl.wait_for_reply(pattern="approve", after_ts="1.0")
    assert result == "APPROVE 42"


def test_wait_for_reply_times_out_and_returns_none(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.conversations_history.return_value = {"messages": []}
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    times = iter([0, 0, 1, 2, 100])
    monkeypatch.setattr(sl.time, "monotonic", lambda: next(times))
    sleep_mock = MagicMock()
    monkeypatch.setattr(sl.time, "sleep", sleep_mock)

    result = sl.wait_for_reply(pattern="approve", after_ts="1.0", timeout_s=10, poll_s=1)

    assert result is None
    assert sleep_mock.call_count >= 1


def test_wait_for_reply_default_timeout_from_settings(monkeypatch):
    settings = _fake_settings(approval_timeout_s=42)
    monkeypatch.setattr(sl, "load_settings", lambda: settings)
    fake_client = MagicMock()
    fake_client.conversations_history.return_value = {"messages": []}
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    times = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(sl.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(sl.time, "sleep", MagicMock())

    result = sl.wait_for_reply(pattern="approve", after_ts="1.0")
    assert result is None


def test_wait_for_reply_returns_none_on_slack_api_error(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.conversations_history.side_effect = _slack_api_error()
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)

    result = sl.wait_for_reply(pattern="approve", after_ts="1.0")
    assert result is None


def test_wait_for_reply_skips_message_matching_after_ts(monkeypatch):
    monkeypatch.setattr(sl, "load_settings", lambda: _fake_settings())
    fake_client = MagicMock()
    fake_client.conversations_history.return_value = {
        "messages": [{"ts": "1.0", "text": "approve (this is the report itself)", "user": "U1"}]
    }
    monkeypatch.setattr(sl, "_client_instance", lambda: fake_client)
    monkeypatch.setattr(sl.time, "sleep", MagicMock())

    times = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(sl.time, "monotonic", lambda: next(times))

    result = sl.wait_for_reply(pattern="approve", after_ts="1.0", timeout_s=10)
    assert result is None


# --- tool wrappers -----------------------------------------------------------


def test_slack_post_message_tool_delegates_to_plain_function(monkeypatch):
    called = {}

    def fake_post_message(text, thread_ts=None):
        called["args"] = (text, thread_ts)
        return "ts-1"

    monkeypatch.setattr(sl, "post_message", fake_post_message)

    result = sl.slack_post_message("hi there", thread_ts="7.0")
    assert result == "ts-1"
    assert called["args"] == ("hi there", "7.0")


def test_slack_post_report_tool_delegates_to_plain_function(monkeypatch):
    called = {}

    def fake_post_report(report, thread_ts=None):
        called["args"] = (report, thread_ts)
        return "ts-2"

    monkeypatch.setattr(sl, "post_report", fake_post_report)

    result = sl.slack_post_report({"headline": "ok"}, thread_ts=None)
    assert result == "ts-2"
    assert called["args"] == ({"headline": "ok"}, None)
