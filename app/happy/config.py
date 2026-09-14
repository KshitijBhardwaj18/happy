"""Single source of configuration for Happy.

Local runs read `.env` (repo root or cwd). On AgentCore Runtime the non-secret values come
from `agentcore.json` envVars, and the two tokens are resolved from AgentCore Identity
credential providers when `use_identity` is true. Nothing else in the codebase may read
os.environ directly.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_dotenv() -> str | None:
    here = Path(__file__).resolve()
    for parent in [Path.cwd(), *here.parents]:
        candidate = parent / ".env"
        if candidate.exists():
            return str(candidate)
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_find_dotenv(), env_file_encoding="utf-8", extra="ignore")

    # AWS
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")

    # Models
    model_orchestrator: str = Field(default="us.anthropic.claude-sonnet-4-6", alias="HAPPY_MODEL_ORCHESTRATOR")
    model_specialist: str = Field(default="global.anthropic.claude-sonnet-4-5-20250929-v1:0", alias="HAPPY_MODEL_SPECIALIST")

    # Cluster
    kubeconfig_b64: str = Field(default="", alias="KUBECONFIG_B64")
    watch_namespaces: list[str] = Field(default=["shop"], alias="HAPPY_WATCH_NAMESPACES")

    # Slack
    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_channel_id: str = Field(default="", alias="SLACK_CHANNEL_ID")

    # GitHub
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_repo: str = Field(default="KshitijBhardwaj18/happy", alias="GITHUB_REPO")

    # AgentCore
    memory_id: str = Field(default="", alias="MEMORY_HAPPYMEMORY_ID")
    gateway_url: str = Field(default="", alias="HAPPY_GATEWAY_URL")
    actor_id: str = Field(default="sre-team", alias="HAPPY_ACTOR_ID")
    use_identity: bool = Field(default=False, alias="HAPPY_USE_IDENTITY")
    use_code_interpreter: bool = Field(default=True, alias="HAPPY_USE_CODE_INTERPRETER")
    slack_credential_name: str = Field(default="slack-bot-token", alias="HAPPY_SLACK_CREDENTIAL")
    github_credential_name: str = Field(default="github-token", alias="HAPPY_GITHUB_CREDENTIAL")

    # Local state
    ledger_dir: Path = Field(default=Path(".ledger"), alias="HAPPY_LEDGER_DIR")
    sessions_dir: Path = Field(default=Path(".sessions"), alias="HAPPY_SESSIONS_DIR")
    audit_path: Path = Field(default=Path("audit.jsonl"), alias="HAPPY_AUDIT_PATH")

    # Behaviour
    safe_scale_max: int = Field(default=5, alias="HAPPY_SAFE_SCALE_MAX")
    approval_timeout_s: int = Field(default=600, alias="HAPPY_APPROVAL_TIMEOUT_S")
    recovery_wait_s: int = Field(default=300, alias="HAPPY_RECOVERY_WAIT_S")

    @property
    def has_cluster(self) -> bool:
        return bool(self.kubeconfig_b64)

    @property
    def has_slack(self) -> bool:
        return bool(self.slack_bot_token and self.slack_channel_id)

    @property
    def has_memory(self) -> bool:
        return bool(self.memory_id)


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """For tests."""
    load_settings.cache_clear()
