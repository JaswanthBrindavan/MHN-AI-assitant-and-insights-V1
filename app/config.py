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
    # Extended thinking is MODEL-GATED: "adaptive" requires a 4.6+ Anthropic
    # model. Haiku 4.5 and every OpenAI-compatible endpoint reject it with a
    # 400, so it stays off unless the configured model is known to support it.
    llm_thinking: str = "off"
    # Reply language: "auto" mirrors the user's language; or a fixed BCP-47ish
    # code ("en", "hi").
    reply_language: str = "auto"

    # Chat engine: "legacy" (deterministic handler chain) | "agentic" (the LLM
    # orchestrates the same abilities as tools). Both ship; legacy is the
    # default until the agentic engine has proven itself in staging.
    chat_engine: str = "legacy"
    # Tool-call rounds before the agent is forced to answer in text. A bound,
    # not a target — the loop must always terminate.
    llm_max_tool_rounds: int = 3
    # Clarifying questions the assistant may ask per session before it must
    # answer with what it has.
    chat_max_clarifying_questions: int = 2

    # --- erasure and retention -------------------------------------------
    # Days between a "forget me" request and the rows actually being
    # destroyed. The window exists so an accidental or coerced deletion can be
    # undone; the data stops being USED immediately either way. Fixed on the
    # request row at request time, so changing this never moves a promise
    # already made.
    erasure_grace_days: int = 30

    # How long chat transcript is kept. This is the bloat: derived, messages
    # and receipts are 97.5% of Davi-owned per-user bytes, 9.94 TB/yr at 10M
    # users, with no cap before this existed.
    message_retention_days: int = 180

    # Receipts are kept LONGER than messages on purpose. They hash the message
    # rather than storing it, so they carry no PHI, and they are the actual
    # audit trail — what was asked (as a hash), which model answered, what was
    # retrieved, whether grounding passed. Keeping the audit while dropping the
    # content is the whole point of the split.
    receipt_retention_days: int = 400

    # 0 disables a sweep entirely, for an operator who wants to stage it.
    retention_batch_size: int = 5000

    # Embeddings
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_dim: int = 1024

    # Grounding: off | log | enforce
    grounding_mode: str = "log"

    # Pipeline
    pipeline_version: int = 1
    app_env: str = "dev"

    # Self-hosted translation sidecar (translator/ in this repo — AI4Bharat
    # IndicTrans2 + IndicXlit + IndicLID). Empty base URL = English-pivot
    # translation disabled; non-English messages then fall back to the
    # reply-language directive on the LLM. PHI only ever goes to OUR sidecar.
    translate_base_url: str = ""
    translate_token: str = ""
    translate_timeout_seconds: float = 8.0

    # mhn-ai pipeline trigger for chat uploads (Davi handles no document
    # bytes or rows — files reach S3 + unclassified_files via Spring):
    # POST {base}/v1/document-processing-runs with this bearer token — the
    # SAME value as mhn-ai's MHN_SERVICE_TOKEN (verified contract, see
    # app/documents/service.py). Empty base URL = trigger disabled (the chat
    # turn still succeeds; documents stay unprocessed and retryable).
    mhn_ai_base_url: str = ""
    mhn_ai_token: str = ""
    mhn_ai_timeout_seconds: float = 10.0

    # Spring file access for vision. Davi still holds NO AWS credentials: it
    # asks Spring — which owns the bucket and already authorizes file reads —
    # to mint a short-lived presigned GET, then reads those bytes in memory for
    # one turn. Empty base URL = vision disabled; the chat falls back to the
    # extracted content.ai it has always used.
    mhn_spring_base_url: str = ""
    mhn_spring_token: str = ""
    mhn_spring_timeout_seconds: float = 15.0

    # Vision. Only reached when a document fetch succeeded, so it is gated by
    # the same consent checks as every other family read.
    vision_enabled: bool = False
    vision_model: str = ""

    # Voice. Self-hosted sidecar, same pattern as translator/ — PHI never
    # leaves the deployment. Empty base URL = voice disabled.
    voice_base_url: str = ""
    voice_token: str = ""
    voice_timeout_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
