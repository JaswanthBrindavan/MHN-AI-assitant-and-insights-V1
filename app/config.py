"""Application configuration, loaded from environment (.env in dev)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://davi:davi@localhost:5432/davi"
    alembic_database_url: str = "postgresql+psycopg2://davi:davi@localhost:5432/davi"

    # Auth
    auth_enabled: bool = False
    jwt_secret: str = "change-me-in-prod"
    jwt_algorithm: str = "HS256"

    # LLM
    llm_provider: str = "fake"
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "llama3.1"
    llm_prompt_version: str = "v1"

    # Embeddings
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_dim: int = 1024

    # Grounding: off | log | enforce
    grounding_mode: str = "log"

    # Pipeline
    pipeline_version: int = 1
    app_env: str = "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
