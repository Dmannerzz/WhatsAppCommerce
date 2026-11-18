from functools import lru_cache
from pathlib import Path

from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: PostgresDsn = (
        "postgresql+asyncpg://whatsapp:change-me-for-local-development"
        "@localhost:5432/whatsapp_agent"
    )
    groq_api_key: SecretStr | None = None
    llm_model: str = "llama-3.3-70b-versatile"
    wholesale_group_jid: str | None = None
    retail_group_jid: str | None = None
    mum_admin_jid: str | None = None
    markup_rule: int = 2500
    min_profit_rule: int = 500
    bridge_webhook_secret: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
