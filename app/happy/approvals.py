"""The approval loop: resolve a Strands interrupt through a human reply in Slack.

`hands.py` tools raise an interrupt (via `guardrails.require_approval`) instead of
acting on risky tiers. `run_with_approvals` is the other half: it drives the agent
through as many rounds of interrupts as it takes, posting one message per interrupt in
the incident's Slack thread and blocking on a matching human reply before resuming.
"""
from __future__ import annotations

import logging
import re

from config import load_settings
from ledger import get_ledger
from tools.slack import post_message, wait_for_reply

logger = logging.getLogger(__name__)

_SHORT_ID_LEN = 6


def _short_id(interrupt_id: str) -> str:
    return interrupt_id[:_SHORT_ID_LEN]


def _deny_reason(reply: str, short_id: str) -> str:
    """Strip the leading "deny <id>" (if present) off a reply, leaving just the reason."""
    text = re.sub(r"(?i)^\s*deny\b", "", reply)
    text = re.sub(re.escape(short_id), "", text, flags=re.I)
    return text.strip()


def run_with_approvals(agent, prompt, thread_ts: str | None = None, ledger=None):
    """Run `agent(prompt)`, resolving every approval interrupt via Slack before returning.

    Strands stops the agent loop (`result.stop_reason == "interrupt"`) whenever a tool
    calls `guardrails.require_approval`. For each pending interrupt this posts a message
    naming a short id (the interrupt id's first 6 characters) into `thread_ts`, then
    polls `wait_for_reply` for a human message containing that id. A reply that does not
    arrive within the approval timeout is treated as a deny with reason "timed out". Any
    deny whose reason is longer than three words is remembered as a standing preference
    (`ledger.remember_preference`) so future proposals can take it into account. Once a
    reply is in hand for every pending interrupt, the agent is resumed with those
    responses; this repeats until the agent finishes normally. Returns the final
    `AgentResult`.
    """
    ledger = ledger or get_ledger()
    settings = load_settings()
    result = agent(prompt)

    while result.stop_reason == "interrupt":
        responses = []
        for interrupt in result.interrupts:
            short_id = _short_id(interrupt.id)
            reason = interrupt.reason or {}
            action = reason.get("action", "action")
            details = reason.get("details", {})

            after_ts = post_message(
                f'🟡 Approval needed [{short_id}]: {action} {details}. '
                f'Reply "approve {short_id}" or "deny {short_id} <reason>".',
                thread_ts=thread_ts,
            )
            reply = wait_for_reply(
                pattern=re.escape(short_id),
                after_ts=after_ts or (thread_ts or "0"),
                timeout_s=settings.approval_timeout_s,
                thread_ts=thread_ts,
            )

            if reply is None:
                reply = f"deny {short_id} timed out"
                logger.info("Approval %s timed out; treating as deny", short_id)

            approved = reply.strip().lower().startswith("approve")
            if not approved:
                deny_reason = _deny_reason(reply, short_id)
                if len(deny_reason.split()) > 3:
                    ledger.remember_preference(f"Denied {action} on {details}: {deny_reason}")

            responses.append({"interruptResponse": {"interruptId": interrupt.id, "response": reply}})

        result = agent(responses)

    return result
