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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.amenities import DEFAULT_AMENITIES, AmenityGroup, parse_amenities

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
    # Where documents come from:
    #   dropbox: the board keeps them in a Dropbox folder; the portal reads and indexes
    #            them, and the subfolder a file is in decides who can see it.
    #   upload:  people upload through the portal; files go to STORAGE_BACKEND.
    document_source: Literal["upload", "dropbox"] = "upload"
    dropbox_app_key: str = ""
    dropbox_app_secret: str = _secret()
    dropbox_refresh_token: str = _secret()
    dropbox_root: str = "/HOA Portal"
    # Subfolder (relative to DROPBOX_ROOT) -> groups who can see files in it. A file
    # takes the deepest matching folder; files outside every listed folder are ignored.
    dropbox_folder_roles: str = json.dumps(
        {
            "Everyone": ["board", "management", "homeowner"],
            "Board and Management": ["board", "management"],
            "Board": ["board"],
            "Management": ["management"],
        }
    )
    dropbox_sync_minutes: int = 10
    dropbox_max_file_mb: int = 50

    # Storage for portal uploads (DOCUMENT_SOURCE=upload).
    storage_backend: Literal["local", "s3"] = "local"
    # Master key for encrypting uploaded files on disk. 32 bytes, urlsafe base64.
    # Generate: python -m app.cli generate-key
    storage_encryption_key: str = _secret()
    local_storage_dir: str = "./var/documents"
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

    # --- Reservations -------------------------------------------------------
    # Local time zone for opening hours and calendars (an IANA name).
    hoa_timezone: str = "America/New_York"
    # Bookable amenities and their rules; see app/amenities.py for every option.
    amenities: str = json.dumps(DEFAULT_AMENITIES)

    seed_demo_password: str = Field(default="", repr=False)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def dropbox_folder_map(self) -> list[tuple[str, list[str]]]:
        """(lower-cased folder path, roles), deepest folders first."""
        return _parse_folder_roles(self.dropbox_folder_roles)

    @property
    def amenity_groups(self) -> list[AmenityGroup]:
        return _amenity_groups(self.amenities)

    @property
    def management_notify_list(self) -> list[str]:
        return [e.strip() for e in self.management_notify_emails.split(",") if e.strip()]

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        _parse_folder_roles(self.dropbox_folder_roles)  # fail fast on a bad mapping
        _amenity_groups(self.amenities)
        try:
            ZoneInfo(self.hoa_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"HOA_TIMEZONE {self.hoa_timezone!r} isn't a known time zone") from exc
        if self.app_env != "production":
            return self
        problems = []
        uploads = self.document_source == "upload"
        if self.secret_key == DEV_SECRET or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set to a random value of 32+ characters")
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true")
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL must point at Postgres")
        if uploads and self.storage_backend == "s3" and not (self.s3_bucket and self.s3_kms_key_id):
            problems.append("S3_BUCKET and S3_KMS_KEY_ID are required for the s3 backend")
        if uploads and self.storage_backend == "local" and not self.storage_encryption_key:
            problems.append("STORAGE_ENCRYPTION_KEY is required for the local storage backend")
        if self.document_source == "dropbox" and not (
            self.dropbox_app_key and self.dropbox_app_secret and self.dropbox_refresh_token
        ):
            problems.append(
                "DROPBOX_APP_KEY, DROPBOX_APP_SECRET and DROPBOX_REFRESH_TOKEN are required "
                "when DOCUMENT_SOURCE=dropbox"
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


def _parse_folder_roles(raw: str) -> list[tuple[str, list[str]]]:
    shareable = {"board", "management", "homeowner"}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError("DROPBOX_FOLDER_ROLES must be a JSON object") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("DROPBOX_FOLDER_ROLES must be a non-empty JSON object")
    out = []
    for folder, roles in data.items():
        path = "/".join(p for p in str(folder).strip().lower().split("/") if p)
        if not path or not isinstance(roles, list) or not roles:
            raise ValueError(f"DROPBOX_FOLDER_ROLES: bad entry for {folder!r}")
        bad = set(roles) - shareable
        if bad:
            raise ValueError(f"DROPBOX_FOLDER_ROLES: unknown group(s) {sorted(bad)} for {folder!r}")
        out.append((path, sorted(set(roles))))
    return sorted(out, key=lambda item: item[0].count("/"), reverse=True)


@lru_cache(maxsize=8)
def _amenity_groups(raw: str) -> list[AmenityGroup]:
    return parse_amenities(raw)


@lru_cache
def get_settings() -> Settings:
    return Settings()
