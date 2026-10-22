"""Runtime configuration, read from VIBEVAULT_* environment variables (or a local .env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_TOKEN_LENGTH = 16
_PLACEHOLDERS = {"change-me", "changeme", "replace-me", "your-token-here"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIBEVAULT_", env_file=".env", extra="ignore")

    # --- core -------------------------------------------------------------
    database_url: str = "sqlite:///./data/vibevault.db"
    auto_migrate: bool = True
    log_level: str = "INFO"
    log_json: bool = True
    static_dir: str | None = None  # built frontend; served at "/" when present
    cors_origins: str = ""  # comma separated; empty = same-origin only

    # --- authentication ---------------------------------------------------
    # Admin token: full read/write access (used by the web UI and operators).
    admin_token: SecretStr | None = None
    # Ingest tokens: "name=token,name2=token2". Can only POST events.
    ingest_tokens: SecretStr | None = None
    # Read tokens: "name=token,...". Read-only: GET projects/sessions/events/search (no writes, jobs, audit, derived).
    read_tokens: SecretStr | None = None

    # --- request limits ---------------------------------------------------
    max_body_bytes: int = 8_000_000
    max_batch_events: int = 500
    max_event_content_bytes: int = 1_000_000
    max_metadata_bytes: int = 256_000
    max_raw_payload_bytes: int = 2_000_000

    # --- ingestion behaviour ---------------------------------------------
    auto_create_projects: bool = True
    # An unknown project key whose name equals an existing project's name joins that project (the same
    # repository on another machine, a repo created for an idea first discussed in a chat, ...).
    match_projects_by_name: bool = True
    server_redaction: bool = True  # defence in depth; collectors redact first
    redaction_rules_file: str | None = None  # JSON list of {"name","pattern"}

    # --- background jobs --------------------------------------------------
    worker_enabled: bool = True
    worker_poll_seconds: float = 2.0
    job_max_attempts: int = 5
    job_backoff_base_seconds: float = 30.0
    job_backoff_max_seconds: float = 3600.0
    job_lease_seconds: int = 300
    job_batch_size: int = 200  # events handled per job step
    job_step_budget_seconds: float = 20.0  # wall-clock budget per job step
    jobs_per_tick: int = 10
    sweep_interval_seconds: float = 30.0

    # --- AI providers (all optional) -------------------------------------
    providers: str = ""  # ordered list, e.g. "local,groq,gemini"
    allow_external_ai: bool = False  # must be true before any non-local provider is used
    auto_enqueue_ai: bool = False  # enqueue summarize_session automatically

    # --- project classification (job classify_sessions; needs a provider) ---
    classify_at: str = ""  # local time "HH:MM" to run nightly (empty = only on demand)
    timezone: str = "Europe/Warsaw"  # for classify_at
    classify_batch: int = 50  # sessions per run
    classify_max_chars: int = 16_000  # conversation text per session (start + end kept); halved on HTTP 413
    classify_auto_apply_confidence: float = 0.85  # existing-project answers at/above this are applied

    @field_validator("classify_at")
    @classmethod
    def _check_classify_at(cls, v: str) -> str:
        v = v.strip()
        if v:
            hh, _, mm = v.partition(":")
            if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                raise ValueError("VIBEVAULT_CLASSIFY_AT must look like HH:MM")
        return v

    @model_validator(mode="after")
    def _check_tokens(self) -> Settings:
        if self.admin_token is not None:
            tok = self.admin_token.get_secret_value()
            if len(tok) < MIN_TOKEN_LENGTH or tok.lower() in _PLACEHOLDERS:
                raise ValueError(
                    f"VIBEVAULT_ADMIN_TOKEN must be at least {MIN_TOKEN_LENGTH} characters "
                    "and not a placeholder (generate one with `openssl rand -hex 32`)"
                )
        for name, tok in self.parsed_ingest_tokens().items():
            if len(tok) < MIN_TOKEN_LENGTH or tok.lower() in _PLACEHOLDERS:
                raise ValueError(f"ingest token '{name}' is too short or a placeholder")
        for name, tok in self.parsed_read_tokens().items():
            if len(tok) < MIN_TOKEN_LENGTH or tok.lower() in _PLACEHOLDERS:
                raise ValueError(f"read token '{name}' is too short or a placeholder")
        return self

    def parsed_read_tokens(self) -> dict[str, str]:
        """Return {token_name: token} for read-only tokens (names are used for audit logs only)."""
        out: dict[str, str] = {}
        raw = self.read_tokens.get_secret_value() if self.read_tokens else ""
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError("VIBEVAULT_READ_TOKENS entries must look like name=token")
            name, tok = part.split("=", 1)
            out[name.strip()] = tok.strip()
        return out

    def parsed_ingest_tokens(self) -> dict[str, str]:
        """Return {token_name: token}. Names are used for audit logs only."""
        out: dict[str, str] = {}
        raw = self.ingest_tokens.get_secret_value() if self.ingest_tokens else ""
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError("VIBEVAULT_INGEST_TOKENS entries must look like name=token")
            name, tok = part.split("=", 1)
            out[name.strip()] = tok.strip()
        return out

    def provider_names(self) -> list[str]:
        return [p.strip().lower() for p in self.providers.split(",") if p.strip()]

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
