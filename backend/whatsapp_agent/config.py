import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = (
        "postgresql+asyncpg://whatsapp:change-me-for-local-development"
        "@localhost:5432/whatsapp_agent"
    )
    postgres_url: str | None = None
    postgres_url_non_pooling: str | None = None
    groq_api_key: SecretStr | None = None
    llm_model: str = "llama-3.3-70b-versatile"
    vision_model: str = "qwen/qwen3.8-27b"
    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    bridge_api_url: str = "http://localhost:3001"
    bridge_transport: Literal["http", "database"] = "http"
    bridge_api_secret: SecretStr | None = None
    wholesale_group_jid: str | None = None
    wholesale_group_jids: str = ""
    retail_group_jid: str | None = None
    mum_admin_jid: str | None = None
    wholesaler_jids: str = ""
    markup_rule: int = 2500
    min_profit_rule: int = 500
    bridge_webhook_secret: SecretStr | None = None
    media_dir: Path = Path("media")

    @model_validator(mode="after")
    def _resolve_database_url(self) -> "Settings":
        """Prefer POSTGRES_URL / POSTGRES_URL_NON_POOLING from the Vercel
        Neon integration when DATABASE_URL was not explicitly set."""
        explicit_database_url = os.environ.get("DATABASE_URL")
        if explicit_database_url:
            return self
        candidate = self.postgres_url_non_pooling or self.postgres_url
        if candidate:
            self.database_url = candidate
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
