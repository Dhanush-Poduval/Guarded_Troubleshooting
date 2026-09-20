"""Application settings, loaded from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- PostgreSQL ---
    # Defaults mirror the compose fallbacks so the app works before a .env is written.
    postgres_user: str = "troubleshoot_user"
    postgres_password: str = "troubleshoot_password"
    postgres_db: str = "troubleshooting"
    postgres_host: str = "localhost"
    postgres_port: int = 5433

    # --- Embedding model ---
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # pgvector can store up to 16000 dimensions but only indexes `vector` columns with
    # HNSW up to 2000, so anything larger would silently lose the index.
    embedding_dim: int = Field(default=384, ge=1, le=2000)

    @property
    def database_url(self) -> str:
        """libpq connection URL. Credentials are percent-encoded."""
        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def safe_database_url(self) -> str:
        """Same URL with the password masked, for logs and error messages."""
        user = quote_plus(self.postgres_user)
        return (
            f"postgresql://{user}:***"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
