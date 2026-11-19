"""Application settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-insecure-secret-change-me"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- App ---------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    hoa_name: str = "HOA Portal"
    base_url: str = "http://localhost:8000"
    secret_key: str = DEV_SECRET
    database_url: str = "sqlite:///./dev.db"

    # --- Sessions & tokens -------------------------------------------------
    session_cookie_name: str = "hoa_session"
    session_cookie_secure: bool = False
    session_ttl_hours: int = 24 * 14
    invite_ttl_hours: int = 24 * 7
    password_reset_ttl_minutes: int = 60
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    min_password_length: int = 12

    # --- Storage -----------------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: str = "./var/documents"
    # 32-byte key, urlsafe base64. Generate: python -m app.cli generate-key
    local_storage_key: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "documents/"
    s3_kms_key_id: str = ""  # KMS key id/ARN/alias for SSE-KMS
    s3_endpoint_url: str = ""  # optional, e.g. for S3-compatible stores
    max_upload_mb: int = 25

    # --- Embeddings ---------------------------------------------------------
    embedding_provider: Literal["hashing", "voyage"] = "hashing"
    embedding_dim: int = 1024
    voyage_api_key: str = ""
    voyage_model: str = "voyage-3.5"

    # --- Chat / RAG ---------------------------------------------------------
    llm_provider: Literal["anthropic", "offline"] = "anthropic"
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    anthropic_max_tokens: int = 4000
    anthropic_fallbacks: bool = True  # server-side refusal fallback
    rag_top_k: int = 8
    rag_min_score: float = 0.15
    rag_history_turns: int = 6
    chunk_size_chars: int = 1800
    chunk_overlap_chars: int = 250

    # --- Email --------------------------------------------------------------
    email_provider: Literal["console", "smtp", "ses", "postmark"] = "console"
    email_from: str = "HOA Portal <no-reply@example.com>"
    email_reply_to: str = ""
    # Process the outbox in-request after commit. Fine for dev and small HOAs;
    # in production prefer running `python -m app.worker` and set this false.
    email_send_inline: bool = True
    email_max_attempts: int = 5
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    ses_region: str = "us-east-1"
    ses_configuration_set: str = ""
    postmark_server_token: str = ""
    postmark_message_stream: str = "outbound"
    postmark_broadcast_stream: str = "broadcast"

    # --- Escalation ---------------------------------------------------------
    # Extra addresses to notify on new tickets (e.g. the management company's
    # shared inbox), comma separated. Management users are always notified.
    management_notify_emails: str = ""
    board_can_view_tickets: bool = False

    seed_demo_password: str = Field(default="", repr=False)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def management_notify_list(self) -> list[str]:
        return [e.strip() for e in self.management_notify_emails.split(",") if e.strip()]

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        if self.app_env != "production":
            return self
        problems = []
        if self.secret_key == DEV_SECRET or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set to a random value of 32+ characters")
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true")
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL must point at Postgres")
        if self.storage_backend == "s3" and not (self.s3_bucket and self.s3_kms_key_id):
            problems.append("S3_BUCKET and S3_KMS_KEY_ID are required for the s3 backend")
        if self.storage_backend == "local" and not self.local_storage_key:
            problems.append("LOCAL_STORAGE_KEY is required for the local backend")
        if self.email_provider == "console":
            problems.append("EMAIL_PROVIDER must be a real provider (smtp, ses or postmark)")
        if self.embedding_provider == "voyage" and not self.voyage_api_key:
            problems.append("VOYAGE_API_KEY is required when EMBEDDING_PROVIDER=voyage")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
