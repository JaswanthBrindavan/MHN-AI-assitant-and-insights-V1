"""Application configuration, loaded from environment (.env in dev)."""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("davi.config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://davi:davi@localhost:5432/davi"
    alembic_database_url: str = "postgresql+psycopg2://davi:davi@localhost:5432/davi"
    # Connection pool. SQLAlchemy's defaults are 5 + 10 overflow = 15 — the
    # figure the scaling note derived ~167 concurrent connections at 1M users
    # against. Kept as the default so nothing changes without a decision, but
    # settable, because the database is shared with mhn-spring and mhn-ai and
    # the right number is an operational call, not a code change.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30

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

    # Chat engine: "legacy" (deterministic handler chain) | "agentic" (the LLM
    # orchestrates the same abilities as tools). Both ship; legacy is the
    # default until the agentic engine has proven itself in staging.
    chat_engine: str = "legacy"
    # Tool-call rounds before the agent is forced to answer in text. A bound,
    # not a target — the loop must always terminate.
    # 3 -> 2. Each round is a SEQUENTIAL model call, and measured wall clock is
    # round-count x per-call latency and essentially nothing else (all non-model
    # work in a turn measures 75-310 ms). Worst case drops from 5 calls to 3.
    #
    # NOT 1: two tools document a second round in their own descriptions
    # (ANALYZE_IMAGE says "get the document id from get_documents first"), and
    # at 1 the budget is exhausted before the chained call can run.
    # Measured in staging, per reply: a turn that makes NO model call returns
    # in 4.6 s; one model call costs ~25 s; two cost ~57 s, clustered within
    # +/-2 s across questions of very different lengths. That flatness is the
    # tell — it is not generation time, which tracks output length. It is an
    # unbounded output budget being spent.
    #
    # 800 tokens is roughly 600 words: far above the three sentences the
    # grounding rules ask for, so a normal answer still finishes on its own and
    # only a runaway one is cut. Bounding it bounds the worst case.
    llm_max_tokens: int = 800

    # There was NO timeout at all. `AnthropicProvider` passed only api_key and
    # base_url, so the SDK defaults applied: read=600 s with max_retries=2 —
    # a stalled call could hold a reader for thirty minutes. Audit R1.
    llm_timeout_seconds: float = 60.0

    # One retry, not the SDK's two. A retry after a timeout costs another full
    # timeout, and the pipeline already fails open to a safe reply.
    llm_max_retries: int = 1

    llm_max_tool_rounds: int = 2
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

    # Symptom history. Kept longer than the transcript and alongside the
    # receipts, because "have I had this before?" is a question about months
    # and seasons, not about last week — a reader asking in March whether they
    # had this last winter is asking something clinically ordinary. The rows
    # are small and coarse (a symptom term, the floor's level, the terms that
    # matched), so a longer window costs little and answers much. Shorten it
    # here if a deployment's policy says otherwise.
    symptom_retention_days: int = 400

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

    @model_validator(mode="after")
    def _no_open_door_outside_dev(self) -> Settings:
        """Refuse to start a non-dev deployment with auth off or a default
        secret.

        auth_enabled defaults to False for local work, and with auth off a
        bare ``X-User-Id: <any uuid>`` header IS the identity — on a service
        that shares the MHN production database, one forgotten env var would
        let anyone read any user's chat history and profile. A misconfigured
        deploy must die at startup, not serve.
        """
        if self.app_env != "dev":
            problems = []
            if not self.auth_enabled:
                problems.append(
                    "AUTH_ENABLED must be true (X-User-Id impersonation "
                    "otherwise)"
                )
            if self.jwt_secret == "change-me-in-prod":
                problems.append("JWT_SECRET is still the built-in default")
            if problems:
                raise ValueError(
                    f"unsafe configuration for APP_ENV={self.app_env!r}: "
                    + "; ".join(problems)
                )
        if self.service_token and len(self.service_token) < 32:
            # The auth layer refuses tokens under 32 chars — a short one is
            # a service path that LOOKS configured and silently is not.
            logger.warning(
                "SERVICE_TOKEN is set but shorter than 32 characters — the "
                "server-to-server path is DISABLED until it is lengthened"
            )
        # Say which mode we are in, so a dev-auth deploy is visible in the
        # very first log lines rather than discovered from behavior.
        logger.info(
            "auth mode: %s (app_env=%s)",
            "enforced" if self.auth_enabled else "DISABLED — dev only",
            self.app_env,
        )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
