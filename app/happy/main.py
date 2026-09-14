"""AgentCore Runtime entrypoint for Happy.

Two payload shapes:
  {"mode": "patrol" | "digest" | "handoff", "args": {...}}
      EventBridge Scheduler's shape. Starts the matching runner as a tracked background
      task and acks immediately (`{"status": "accepted", "mode": mode}`) so the
      scheduler sees a fast response while the run continues in the background.
  {"prompt": "..."}
      An ad-hoc question, answered synchronously by the patrol agent.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agent import build_happy

app = BedrockAgentCoreApp()
log = app.logger

_RUNNER_MODES = {"patrol", "digest", "handoff"}


def strip_trailing_tool_use(messages: Any) -> list[dict]:
    """Strip toolUse blocks from the tail until the last message has none.

    Carried over from the generated scaffold: harmless, cheap, and useful if Happy ever
    needs to accept a raw `messages` payload shape again.
    """
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")

    messages = list(messages)
    while messages:
        last = messages[-1]
        if not isinstance(last, dict):
            raise ValueError("each message must be an object")
        original_content = last.get("content", [])
        if not isinstance(original_content, list) or not all(isinstance(block, dict) for block in original_content):
            raise ValueError("each message content value must be a list of content blocks")

        content = [block for block in original_content if "toolUse" not in block]
        if len(content) == len(original_content):
            break
        if content:
            messages[-1] = {**last, "content": content}
            break
        messages.pop()

    return messages


def _run_mode_sync(mode: str, args: dict) -> dict:
    """Run one of the runner functions synchronously (imported lazily so a bad import
    in runner.py can't break entrypoint registration)."""
    from runner import run_digest, run_handoff, run_patrol

    if mode == "patrol":
        return run_patrol(session_id=args.get("session_id"))
    if mode == "digest":
        return run_digest()
    if mode == "handoff":
        return run_handoff()
    return {"status": "error", "error": f"unknown mode {mode!r}"}


async def _run_mode_tracked(mode: str, args: dict) -> None:
    """Run `mode` in a thread, as an AgentCore-tracked async task, so
    `agentcore traces`/logs show the run even though the entrypoint already returned."""
    task_id = app.add_async_task(mode)
    try:
        result = await asyncio.to_thread(_run_mode_sync, mode, args)
        log.info("mode=%s completed result=%s", mode, result)
    except Exception:
        log.exception("mode=%s failed", mode)
    finally:
        app.complete_async_task(task_id)


@app.entrypoint
async def invoke(payload: dict, context: Any) -> dict:
    log.info("Happy invoked with payload keys=%s", list(payload) if isinstance(payload, dict) else type(payload))

    # `agentcore invoke '{"mode": "patrol"}'` arrives as {"prompt": "{\"mode\": \"patrol\"}"}; unwrap it.
    if isinstance(payload, dict) and isinstance(payload.get("prompt"), str) and payload["prompt"].lstrip().startswith("{"):
        try:
            inner = json.loads(payload["prompt"])
            if isinstance(inner, dict) and inner.get("mode"):
                payload = inner
        except ValueError:
            pass

    if isinstance(payload, dict) and payload.get("mode"):
        mode = payload["mode"]
        if mode not in _RUNNER_MODES:
            return {"status": "error", "error": f"unknown mode {mode!r}"}
        args = payload.get("args") or {}
        asyncio.create_task(_run_mode_tracked(mode, args))
        return {"status": "accepted", "mode": mode}

    if isinstance(payload, dict) and "prompt" in payload:
        session_id = getattr(context, "session_id", None) or "adhoc"
        try:
            agent = build_happy("patrol", session_id)
            result = await asyncio.to_thread(agent, payload["prompt"])
            return {"result": str(result)}
        except Exception as exc:  # noqa: BLE001
            log.exception("ad-hoc prompt failed")
            return {"status": "error", "error": str(exc)[:400]}

    return {"status": "error", "error": "payload must include 'mode' or 'prompt'"}


if __name__ == "__main__":
    app.run()
