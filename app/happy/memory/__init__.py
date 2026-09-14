"""Session management for Happy: pending approvals and conversation history survive
process restarts.

Note on layout: the AgentCore CLI already generated `memory/session.py` (a helper
around `AgentCoreMemorySessionManager`) as a package, so `build_session_manager` lives
here in `memory/__init__.py` rather than in a sibling `memory.py` -- Python cannot have
both a `memory.py` module and a `memory/` package in the same directory. Importing
`from memory import build_session_manager` works exactly the same either way.
"""
from __future__ import annotations

from strands.session.file_session_manager import FileSessionManager

from config import load_settings
from memory.session import get_memory_session_manager


def build_session_manager(session_id: str):
    """Return a session manager for `session_id`.

    Uses the AgentCore Memory-backed session manager (`memory/session.py`, keyed off
    `MEMORY_HAPPYMEMORY_ID`) when `Settings.has_memory` is true; this is what makes a
    paused approval interrupt, and prior conversation summaries/facts, survive a
    process restart on Runtime. Falls back to a local `FileSessionManager` rooted at
    `Settings.sessions_dir` otherwise, so local runs, demos, and tests work with no AWS
    memory resource configured.
    """
    settings = load_settings()
    if settings.has_memory:
        manager = get_memory_session_manager(session_id, settings.actor_id)
        if manager is not None:
            return manager
    return FileSessionManager(session_id=session_id, storage_dir=str(settings.sessions_dir))
