"""Application settings.

Values come from, in order of precedence: environment variables, an AWS Secrets
Manager secret (when AWS_SECRETS_MANAGER_SECRET_ID is set), a local .env file,
then the defaults below. In production keep every secret in Secrets Manager and
only non-secret settings in the environment.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.amenities import DEFAULT_AMENITIES, AmenityGroup, parse_amenities

DEV_SECRET = "dev-insecure-secret-change-me"  # noqa: S105


SECRETS_ID_ENV = "AWS_SECRETS_MANAGER_SECRET_ID"


@dataclass(frozen=True)
class ModelInfo:
    """What the portal needs to know about a Claude model to call it and count its cost."""

    input_per_mtok: float  # USD per million tokens
    output_per_mtok: float
    # The `thinking` type that turns extended thinking off, or None where it can't be
    # (effort is then the only control). Each of these 400s on the other models.
    thinking_off: str | None
    fallbacks: bool  # server-side refusal fallback (`fallbacks: "default"`)


# Where thinking can't be turned off, max_tokens covers the thinking and the answer
# together; at the usual 1,000 answers would often be cut short.
ALWAYS_THINKING_MIN_TOKENS = 4000

MODELS = {
    # Haiku's price is for prompts up to 100K tokens; the portal's are about 6K.
    "claude-haiku-5-5": ModelInfo(0.10, 0.50, "disabled", fallbacks=False),
    "claude-sonnet-5-5": ModelInfo(2.0, 10.0, "between_tools", fallbacks=True),
    "claude-opus-5-5": ModelInfo(4.0, 20.0, None, fallbacks=True),
}


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
    # hide_input_in_errors: a bad setting must not print the others (secrets included)
    # into the logs.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

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
    # Open a fresh database connection per request instead of keeping a pool. Use it
    # on AWS Lambda with a database that sleeps when idle (Neon): an idle pooled
    # connection would go stale while the database sleeps.
    database_null_pool: bool = False
    # On AWS Lambda: the function that runs background work (email, Dropbox sync).
    # When set, the web function hands that work to it instead of doing it in-request.
    tasks_function_name: str = ""

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
    # Send larger downloads straight from Dropbox (a link that works for 4 hours)
    # instead of through the portal. 0 = never. AWS Lambda responses are capped at
    # 6 MB, so set this to 4 there.
    download_redirect_over_mb: int = 0

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

    # --- Search -------------------------------------------------------------
    # How the assistant finds passages:
    #   keyword: Postgres full-text search (word stems, ranked). Needs no other account.
    #   vector:  embeddings from EMBEDDING_PROVIDER (voyage for real use) and pgvector.
    retrieval: Literal["keyword", "vector"] = "vector"

    # --- Embeddings ---------------------------------------------------------
    embedding_provider: Literal["hashing", "voyage"] = "hashing"
    embedding_dim: int = 1024
    voyage_api_key: str = _secret()
    voyage_model: str = "voyage-3.5"

    # --- Chat / RAG ---------------------------------------------------------
    llm_provider: Literal["anthropic", "offline"] = "anthropic"
    anthropic_api_key: str = _secret()
    # The cheapest current model; answering from a few quoted passages doesn't need more.
    anthropic_model: str = "claude-haiku-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Extended thinking costs output tokens; answers here are lookups, so it's off.
    anthropic_thinking: bool = False
    anthropic_max_tokens: int = 1000  # cap on each answer's output
    anthropic_fallbacks: bool = False  # server-side refusal fallback (not on Haiku)
    # Usage limits. Questions per person per day (HOA_TIMEZONE), and a portal-wide
    # budget per calendar month: once the month's estimated spend reaches it, the
    # assistant pauses until the 1st and points people to management. 0 = no limit.
    chat_daily_limit_per_user: int = 20
    chat_monthly_budget_usd: float = 5.0
    # Prices used for the estimate, in USD per million tokens. Leave them unset for a
    # model in MODELS above and its prices are used; set both for any other model.
    chat_price_input_per_mtok: float | None = None
    chat_price_output_per_mtok: float | None = None
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

    # --- Asking management --------------------------------------------------
    # Extra addresses that get messages homeowners send to management (e.g. the
    # management company's shared inbox), comma separated. Management users always do.
    management_notify_emails: str = ""
    # How many messages one person can send to management in any 24 hours.
    escalations_per_day: int = Field(default=5, ge=1)

    # --- Weekly review --------------------------------------------------------
    # Once a week the portal reviews the duty calendar and recent documents (meeting
    # minutes above all) and emails the board and management their next best actions,
    # only when something is worth acting on. It runs on this day, from this hour
    # (local time) on.
    weekly_review_day: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ] = "monday"
    duty_reminder_hour: int = Field(default=7, ge=0, le=23)
    weekly_review_enabled: bool = True
    # Calendar duties this many days ahead are considered.
    weekly_review_lookahead_days: int = Field(default=21, ge=7, le=90)
    # Documents added or changed this many days back are read; meeting minutes from
    # further back, since their action items run longer.
    weekly_review_document_days: int = Field(default=14, ge=1, le=90)
    weekly_review_minutes_days: int = Field(default=45, ge=1, le=180)
    # Cap on the document text sent per audience (board, management), in characters.
    weekly_review_max_chars: int = Field(default=60_000, ge=5_000, le=400_000)
    weekly_review_max_tokens: int = Field(default=4000, ge=500, le=32_000)

    # --- Inbox to cc ------------------------------------------------------------
    # A dedicated Gmail address board and management members cc on email chains. The
    # portal reads it over IMAP with an app password (read-only: it never sends from it,
    # flags, moves or deletes anything there) and feeds what it reads to the weekly
    # review. Leave INBOX_ADDRESS empty to turn this off. It can be the same Gmail account
    # the portal sends from: with INBOX_ADDRESS equal to SMTP_USERNAME, INBOX_APP_PASSWORD
    # may be left empty and SMTP_PASSWORD is used.
    inbox_address: str = ""
    inbox_app_password: str = _secret()
    inbox_imap_host: str = "imap.gmail.com"
    inbox_imap_port: int = 993
    # The server whose Authentication-Results header is trusted: the inbox's own mail
    # server, which writes the topmost one. Any other is the sender's and is ignored.
    inbox_authserv_id: str = "mx.google.com"
    # Read the inbox this often, and always just before the weekly review. 0 = only then.
    inbox_fetch_hours: int = Field(default=24, ge=0, le=168)
    # Messages that arrived this many days back are looked at on each read.
    inbox_lookback_days: int = Field(default=10, ge=1, le=60)
    # Kept emails are deleted from the portal this many days after they were read in.
    inbox_retention_days: int = Field(default=90, ge=1, le=365)
    inbox_max_message_mb: int = Field(default=10, ge=1, le=50)
    inbox_max_body_chars: int = Field(default=20_000, ge=1_000, le=200_000)

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

    @property
    def inbox_enabled(self) -> bool:
        return bool(self.inbox_address.strip())

    @property
    def inbox_password(self) -> str:
        if self.inbox_app_password:
            return self.inbox_app_password
        same = self.inbox_address.strip().lower() == self.smtp_username.strip().lower()
        return self.smtp_password if same else ""

    @property
    def model_info(self) -> ModelInfo | None:
        return MODELS.get(self.anthropic_model)

    @field_validator("chat_price_input_per_mtok", "chat_price_output_per_mtok", mode="before")
    @classmethod
    def _blank_price(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    def _check_model(self) -> list[str]:
        """Fill in the model's prices, and catch settings the API would reject."""
        info = self.model_info
        chosen = set(self.model_fields_set)  # before anything below fills in a value
        priced = None not in (self.chat_price_input_per_mtok, self.chat_price_output_per_mtok)
        fallback = max(MODELS.values(), key=lambda m: m.output_per_mtok)  # err on the high side
        if self.chat_price_input_per_mtok is None:
            self.chat_price_input_per_mtok = (info or fallback).input_per_mtok
        if self.chat_price_output_per_mtok is None:
            self.chat_price_output_per_mtok = (info or fallback).output_per_mtok
        if self.llm_provider != "anthropic":
            return []
        problems = []
        if info is None:
            if self.is_production and not priced:
                problems.append(
                    f"ANTHROPIC_MODEL {self.anthropic_model!r} isn't one the portal knows the "
                    "prices of; set CHAT_PRICE_INPUT_PER_MTOK and CHAT_PRICE_OUTPUT_PER_MTOK"
                )
            return problems
        if info.thinking_off is None:
            # Thinking stays on. Keep it short unless thinking or an effort was asked for,
            # and leave room for it and the answer. Spend is still counted from usage.
            if not self.anthropic_thinking and "anthropic_effort" not in chosen:
                self.anthropic_effort = "low"
            self.anthropic_max_tokens = max(self.anthropic_max_tokens, ALWAYS_THINKING_MIN_TOKENS)
        if (
            not self.anthropic_thinking
            and info.thinking_off
            and self.anthropic_effort in ("xhigh", "max")
        ):
            problems.append(
                f"{self.anthropic_model} can't turn thinking off at effort "
                f"{self.anthropic_effort}; use ANTHROPIC_EFFORT=high or lower, or "
                "ANTHROPIC_THINKING=true"
            )
        if self.anthropic_fallbacks and not info.fallbacks:
            problems.append(f"{self.anthropic_model} has no server-side fallback")
        return problems

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        _parse_folder_roles(self.dropbox_folder_roles)  # fail fast on a bad mapping
        _amenity_groups(self.amenities)
        try:
            ZoneInfo(self.hoa_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"HOA_TIMEZONE {self.hoa_timezone!r} isn't a known time zone") from exc
        model_problems = self._check_model()
        if self.app_env != "production":
            if model_problems:
                raise ValueError("Bad model settings: " + "; ".join(model_problems))
            return self
        problems = model_problems
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
        if self.email_provider == "smtp" and not self.smtp_host:
            problems.append("SMTP_HOST is required when EMAIL_PROVIDER=smtp")
        if self.email_provider == "smtp" and self.smtp_host.lower().endswith(
            ("gmail.com", "googlemail.com")
        ):
            if not (self.smtp_username and self.smtp_password):
                problems.append(
                    "SMTP_USERNAME and SMTP_PASSWORD (a Gmail app password) are required "
                    "to send through Gmail"
                )
        if self.retrieval == "vector" and self.embedding_provider == "hashing":
            problems.append(
                "EMBEDDING_PROVIDER=hashing is for development; set RETRIEVAL=keyword "
                "or EMBEDDING_PROVIDER=voyage"
            )
        if self.inbox_enabled and not self.inbox_password:
            problems.append(
                "INBOX_APP_PASSWORD is required when INBOX_ADDRESS is set (or make "
                "INBOX_ADDRESS the SMTP_USERNAME account, whose password is then used)"
            )
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
