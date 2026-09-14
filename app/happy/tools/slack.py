"""Slack tools: post messages/reports to the on-call channel, and wait for a
human's approval reply in a thread or channel.

Every function is a safe no-op (logs at INFO, returns None, touches no network)
when `Settings.has_slack` is false -- so tests, dry runs, and demo rehearsals
without a real Slack workspace all still work. `post_message` / `post_report` /
`wait_for_reply` are plain functions used directly by the runner and approvals
loop; `slack_post_message` / `slack_post_report` are thin `@tool` wrappers so
the agent itself can post to Slack.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from strands import tool

from config import load_settings

logger = logging.getLogger(__name__)

_client: WebClient | None = None


def _client_instance() -> WebClient:
    """Return a cached, module-level Slack WebClient, creating it on first use."""
    global _client
    if _client is None:
        settings = load_settings()
        from identity import slack_token

        _client = WebClient(token=slack_token() or settings.slack_bot_token)
    return _client


def _reset_cache() -> None:
    """For tests: drop the cached client so the next call rebuilds it."""
    global _client
    _client = None


def post_message(text: str, thread_ts: str | None = None) -> str | None:
    """Post `text` to `Settings.slack_channel_id`, optionally as a threaded reply.

    Returns the message's `ts` (Slack's timestamp id -- usable as a thread root,
    or as `after_ts` for `wait_for_reply`), or None when Slack isn't configured
    or the API call fails.
    """
    settings = load_settings()
    if not settings.has_slack:
        logger.info("Slack not configured; skipping post_message")
        return None
    try:
        client = _client_instance()
        response = client.chat_postMessage(
            channel=settings.slack_channel_id, text=text, thread_ts=thread_ts
        )
        return response.get("ts")
    except SlackApiError as exc:
        logger.info("post_message failed: %s", exc)
        return None


def post_report(report: Any, thread_ts: str | None = None) -> str | None:
    """Render `report` and post it to Slack.

    Duck-types `report`: calls `report.to_slack_text()` when available (all the
    report models in `models.py` implement it), otherwise falls back to
    `str(report)`. Returns the message ts, or None as described in `post_message`.
    """
    text = report.to_slack_text() if hasattr(report, "to_slack_text") else str(report)
    return post_message(text, thread_ts=thread_ts)


def wait_for_reply(
    pattern: str,
    after_ts: str,
    timeout_s: int | None = None,
    poll_s: int = 5,
    thread_ts: str | None = None,
) -> str | None:
    """Poll Slack until a human message matching `pattern` appears after `after_ts`.

    Polls `conversations.replies` scoped to `thread_ts` when given, else
    `conversations.history` on `Settings.slack_channel_id` (`oldest=after_ts`).
    Messages carrying a `bot_id` or a `subtype` (edits, joins, bot posts, ...)
    are ignored. `pattern` is matched case-insensitively with `re.search`
    against each candidate message's text. `timeout_s` defaults to
    `Settings.approval_timeout_s`. Returns the text of the first matching
    message, or None on timeout, missing Slack config, or an API error.
    """
    settings = load_settings()
    if not settings.has_slack:
        logger.info("Slack not configured; skipping wait_for_reply")
        return None

    timeout = settings.approval_timeout_s if timeout_s is None else timeout_s
    client = _client_instance()
    deadline = time.monotonic() + timeout

    while True:
        try:
            if thread_ts:
                response = client.conversations_replies(
                    channel=settings.slack_channel_id, ts=thread_ts, oldest=after_ts
                )
            else:
                response = client.conversations_history(
                    channel=settings.slack_channel_id, oldest=after_ts
                )
        except SlackApiError as exc:
            logger.info("wait_for_reply failed: %s", exc)
            return None

        for message in response.get("messages", []):
            if message.get("bot_id") or message.get("subtype"):
                continue
            if message.get("ts") == after_ts:
                continue
            text = message.get("text", "")
            if re.search(pattern, text, re.I):
                return text

        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)


@tool
def slack_post_message(text: str, thread_ts: str | None = None) -> str | None:
    """Post a message to the team's Slack on-call channel.

    Use this to notify humans, ask a question, or leave a status update.
    `thread_ts` threads the message under a prior message (e.g. an incident
    report) instead of posting a new top-level message. Returns the posted
    message's ts, or None if Slack is unavailable.
    """
    return post_message(text, thread_ts=thread_ts)


@tool
def slack_post_report(report: dict, thread_ts: str | None = None) -> str | None:
    """Post a structured report (e.g. an incident report or digest) to Slack.

    Formats `report` as Slack markdown before posting. `thread_ts` threads the
    post under a prior message. Returns the posted message's ts, or None if
    Slack is unavailable.
    """
    return post_report(report, thread_ts=thread_ts)
