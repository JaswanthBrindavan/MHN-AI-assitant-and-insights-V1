"""Application configuration, loaded from environment (.env in dev)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://davi:davi@localhost:5432/davi"
    alembic_database_url: str = "postgresql+psycopg2://davi:davi@localhost:5432/davi"

    # Auth — aligned with the mhn-spring production backend:
    #   * session JWTs are signed HS512 with the SAME JWT_SECRET Spring uses,
    #     and Spring Base64-DECODES the secret string before HMAC
    #     (JwtService.getSigningKey: Decoders.BASE64.decode → hmacShaKeyFor).
    #   * jwt_secret_base64=True mirrors that; set False for raw-string secrets
    #     (legacy/dev tokens).
    auth_enabled: bool = False
    jwt_secret: str = "change-me-in-prod"
    jwt_algorithm: str = "HS512"
    jwt_secret_base64: bool = True
    # Optional server-to-server path (mirrors Spring↔mhn-ai's AI_TOKEN /
    # MHN_SERVICE_TOKEN pattern): a caller presenting this static bearer token
    # plus X-User-Id is trusted. Empty = disabled. Must be ≥32 chars when set.
    service_token: str = ""

    # LLM — model/cloud agnostic. llm_provider selects the adapter:
    # fake | openai_compatible | anthropic | ollama (legacy alias).
    llm_provider: str = "fake"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "llama3.1"
    llm_prompt_version: str = "v1"
    # Reply language: "auto" mirrors the user's language; or a fixed BCP-47ish
    # code ("en", "hi").
    reply_language: str = "auto"

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
