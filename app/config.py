"""App settings, read from environment variables (or a local .env file).

Keeping settings here means secrets never live in the code, and the same
code can run on a laptop, in Docker, or in the cloud with different values.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-5-5"
    github_token: str = ""
    max_agent_steps: int = 10
    effort: str = "medium"  # low | medium | high: how hard Claude thinks

    # Abuse / cost protection for the public demo
    new_chats_per_hour: int = 10  # per visitor (IP address)
    messages_per_hour: int = 60  # per visitor
    daily_budget_usd: float = 1.00  # whole server; 0 turns the cap off


@lru_cache
def get_settings() -> Settings:
    return Settings()
