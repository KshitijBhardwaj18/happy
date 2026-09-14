"""AgentCore Identity: Happy keeps no secrets in its config.

When `Settings.use_identity` is true (set on the deployed runtime), Slack and GitHub tokens are
fetched at run time from AgentCore Identity API-key credential providers, using the runtime's
workload identity. Locally, `.env` is used instead. Either way the rest of the code never sees
where the token came from.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from config import load_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def resolve_api_key(provider_name: str) -> str:
    """Fetch an API key from an AgentCore Identity credential provider, or "" on failure."""
    try:
        from bedrock_agentcore.identity.auth import requires_api_key

        @requires_api_key(provider_name=provider_name, into="api_key")
        def _fetch(*, api_key: str = "") -> str:
            return api_key

        key = _fetch()
        logger.info("Resolved credential %s from AgentCore Identity", provider_name)
        return key or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("AgentCore Identity lookup for %s failed (%s); falling back to env", provider_name, exc)
        return ""


def slack_token() -> str:
    s = load_settings()
    if s.use_identity:
        return resolve_api_key(s.slack_credential_name) or s.slack_bot_token
    return s.slack_bot_token


def github_token() -> str:
    s = load_settings()
    if s.use_identity:
        return resolve_api_key(s.github_credential_name) or s.github_token
    return s.github_token
