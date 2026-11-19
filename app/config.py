"""Application settings.

Values come from, in order of precedence: environment variables, an AWS Secrets
Manager secret (when AWS_SECRETS_MANAGER_SECRET_ID is set), a local .env file,
then the defaults below. In production keep every secret in Secrets Manager and
only non-secret settings in the environment.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEV_SECRET = "dev-insecure-secret-change-me"  # noqa: S105


SECRETS_ID_ENV = "AWS_SECRETS_MANAGER_SECRET_ID"


def _secret(**kw) -> Any:
    """A setting that is a secret: never shown in reprs or logs."""
    return Field(default="", repr=False, **kw)


class SecretsManagerSource(PydanticBaseSettingsSource):
    """Loads settings from one AWS Secrets Manager secret holding a JSON object.

    Keys are setting names in upper case, e.g. {"SECRET_KEY": "...", "DATABASE_URL": "..."}.
    The secret is read once at start-up. AWS credentials and region come from the
    usual places (the task or instance role, AWS_REGION).
    """

    def __init__(self, settings_cls: type[BaseSettings], client: Any = None) -> None:
        super().__init__(settings_cls)
        self._client = client
        self._values: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._values is not None:
            return self._values
        secret_id = os.environ.get(SECRETS_ID_ENV, "").strip()
        if not secret_id:
            self._values = {}
            return self._values
        client = self._client
        if client is None:
            import boto3

            client = boto3.client("secretsmanager")
        raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"{SECRETS_ID_ENV} must point at a JSON secret") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{SECRETS_ID_ENV} must point at a JSON object")
        fields = self.settings_cls.model_fields
        self._values = {k.lower(): v for k, v in data.items() if k.lower() in fields}
        return self._values

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._load().get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._load())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            env_settings,
            SecretsManagerSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    # --- App ---------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    hoa_name: str = "HOA Portal"
    base_url: str = "http://localhost:8000"
    secret_key: str = Field(default=DEV_SECRET, repr=False)
    database_url: str = Field(default="sqlite:///./dev.db", repr=False)

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
    storage_backend: Literal["local", "dropbox", "s3"] = "local"
    # Master key for encrypting files before they are stored (local and Dropbox).
    # 32 bytes, urlsafe base64. Generate: python -m app.cli generate-key
    storage_encryption_key: str = _secret()
    local_storage_dir: str = "./var/documents"
    dropbox_app_key: str = ""
    dropbox_app_secret: str = _secret()
    dropbox_refresh_token: str = _secret()
    dropbox_root: str = "/documents"  # inside the app's folder in Dropbox
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "documents/"
    s3_kms_key_id: str = ""  # KMS key id/ARN/alias for SSE-KMS
    s3_endpoint_url: str = ""  # optional, e.g. for S3-compatible stores
    max_upload_mb: int = 25

    # --- Embeddings ---------------------------------------------------------
    embedding_provider: Literal["hashing", "voyage"] = "hashing"
    embedding_dim: int = 1024
    voyage_api_key: str = _secret()
    voyage_model: str = "voyage-3.5"

    # --- Chat / RAG ---------------------------------------------------------
    llm_provider: Literal["anthropic", "offline"] = "anthropic"
    anthropic_api_key: str = _secret()
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
    smtp_password: str = _secret()
    smtp_starttls: bool = True
    ses_region: str = "us-east-1"
    ses_configuration_set: str = ""
    postmark_server_token: str = _secret()
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
        if self.storage_backend in ("local", "dropbox") and not self.storage_encryption_key:
            problems.append("STORAGE_ENCRYPTION_KEY is required for the local and dropbox backends")
        if self.storage_backend == "dropbox" and not (
            self.dropbox_app_key and self.dropbox_app_secret and self.dropbox_refresh_token
        ):
            problems.append(
                "DROPBOX_APP_KEY, DROPBOX_APP_SECRET and DROPBOX_REFRESH_TOKEN are required "
                "for the dropbox backend"
            )
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
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
