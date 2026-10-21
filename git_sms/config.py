"""Runtime configuration, read once from the environment and validated fail-closed."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

RETIRED_LLM_SETTINGS = ("GITHUB_MODELS_TOKEN", "GITHUB_OPENAI_API_KEY", "GITHUB_MODELS_MODEL")
DEVELOPMENT_SECRET = "development-only-secret-key"  # noqa: S105 - refused outside development


class ConfigError(ValueError):
    """Raised when the configuration cannot run safely."""


def _str(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    environment: str = "production"
    public_url: str | None = None
    secret_key: str | None = None
    db_path: str = "git-sms.db"
    users_file: str | None = None

    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_api_key_sid: str | None = None
    twilio_api_key_secret: str | None = None
    twilio_messaging_service_sid: str | None = None
    sender_backend: str = "twilio"
    dev_outbox: str | None = None
    allow_unsigned: bool = False

    github_api_url: str = "https://api.github.com"
    github_token: str | None = None
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    github_app_installation_id: str | None = None

    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    natural_language: bool = True
    retired_llm_settings: tuple[str, ...] = ()

    confirm_writes: bool = True
    confirmation_ttl_seconds: int = 600
    max_commands_per_hour: int = 30
    max_writes_per_day: int = 20
    max_segments_per_reply: int = 3
    max_segments_per_day: int = 300
    max_ai_calls_per_day: int = 200
    retention_days: int = 30
    audit_retention_days: int = 365
    workers: int = 4

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def effective_secret_key(self) -> str:
        return self.secret_key or DEVELOPMENT_SECRET

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        private_key = _str(env, "GITHUB_APP_PRIVATE_KEY")
        key_file = _str(env, "GITHUB_APP_PRIVATE_KEY_FILE")
        if not private_key and key_file:
            try:
                private_key = Path(key_file).read_text()
            except OSError as exc:
                raise ConfigError(f"cannot read GITHUB_APP_PRIVATE_KEY_FILE: {exc.strerror}") from exc
        if private_key and "\\n" in private_key and "\n" not in private_key:
            private_key = private_key.replace("\\n", "\n")

        llm_base_url = _str(env, "LLM_BASE_URL")
        llm_api_key = _str(env, "LLM_API_KEY")
        llm_model = _str(env, "LLM_MODEL", "OPENAI_MODEL")
        openai_key = _str(env, "OPENAI_API_KEY")
        if not llm_api_key and openai_key:
            llm_api_key = openai_key
            llm_base_url = llm_base_url or "https://api.openai.com/v1"

        environment = (_str(env, "GIT_SMS_ENV") or "production").lower()
        return cls(
            environment=environment,
            public_url=(_str(env, "GIT_SMS_PUBLIC_URL") or "").rstrip("/") or None,
            secret_key=_str(env, "GIT_SMS_SECRET_KEY"),
            db_path=_str(env, "GIT_SMS_DB_PATH") or "git-sms.db",
            users_file=_str(env, "GIT_SMS_USERS_FILE"),
            twilio_account_sid=_str(env, "TWILIO_ACCOUNT_SID"),
            twilio_auth_token=_str(env, "TWILIO_AUTH_TOKEN"),
            twilio_api_key_sid=_str(env, "TWILIO_API_KEY_SID"),
            twilio_api_key_secret=_str(env, "TWILIO_API_KEY_SECRET"),
            twilio_messaging_service_sid=_str(env, "TWILIO_MESSAGING_SERVICE_SID"),
            sender_backend=(_str(env, "GIT_SMS_SENDER") or "twilio").lower(),
            dev_outbox=_str(env, "GIT_SMS_DEV_OUTBOX"),
            allow_unsigned=_bool(env, "GIT_SMS_ALLOW_UNSIGNED", False),
            github_api_url=(_str(env, "GITHUB_API_URL") or "https://api.github.com").rstrip("/"),
            github_token=_str(env, "GITHUB_TOKEN"),
            github_app_id=_str(env, "GITHUB_APP_ID"),
            github_app_private_key=private_key,
            github_app_installation_id=_str(env, "GITHUB_APP_INSTALLATION_ID"),
            llm_base_url=llm_base_url.rstrip("/") if llm_base_url else None,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            natural_language=_bool(env, "GIT_SMS_NATURAL_LANGUAGE", True),
            retired_llm_settings=tuple(name for name in RETIRED_LLM_SETTINGS if _str(env, name)),
            confirm_writes=_bool(env, "GIT_SMS_CONFIRM_WRITES", True),
            confirmation_ttl_seconds=_int(env, "GIT_SMS_CONFIRMATION_TTL_SECONDS", 600, minimum=60),
            max_commands_per_hour=_int(env, "GIT_SMS_MAX_COMMANDS_PER_HOUR", 30, minimum=1),
            max_writes_per_day=_int(env, "GIT_SMS_MAX_WRITES_PER_DAY", 20, minimum=0),
            max_segments_per_reply=_int(env, "GIT_SMS_MAX_SEGMENTS_PER_REPLY", 3, minimum=1),
            max_segments_per_day=_int(env, "GIT_SMS_MAX_SEGMENTS_PER_DAY", 300, minimum=1),
            max_ai_calls_per_day=_int(env, "GIT_SMS_MAX_AI_CALLS_PER_DAY", 200, minimum=0),
            retention_days=_int(env, "GIT_SMS_RETENTION_DAYS", 30, minimum=1),
            audit_retention_days=_int(env, "GIT_SMS_AUDIT_RETENTION_DAYS", 365, minimum=1),
            workers=_int(env, "GIT_SMS_WORKERS", 4, minimum=1),
        )

    def problems(self) -> list[str]:
        """Return every reason this configuration must not start; empty means safe."""
        found: list[str] = []
        if self.environment not in {"production", "development"}:
            found.append("GIT_SMS_ENV must be production or development")
        if self.sender_backend not in {"twilio", "console"}:
            found.append("GIT_SMS_SENDER must be twilio or console")

        if not self.is_development:
            if not self.public_url or not self.public_url.startswith("https://"):
                found.append("GIT_SMS_PUBLIC_URL must be the https:// base URL Twilio calls")
            if not self.twilio_auth_token:
                found.append("TWILIO_AUTH_TOKEN is required to verify webhook signatures")
            if not self.twilio_account_sid:
                found.append("TWILIO_ACCOUNT_SID is required")
            if not self.secret_key or len(self.secret_key) < 32:
                found.append("GIT_SMS_SECRET_KEY must be at least 32 characters")
            if not self.users_file:
                found.append("GIT_SMS_USERS_FILE is required")
            if self.sender_backend != "twilio":
                found.append("the console sender is for development only")
            if not self.confirm_writes:
                found.append(
                    "GIT_SMS_CONFIRM_WRITES=false is for development only: the confirmation code is what stops "
                    "a spoofed sender number from changing GitHub"
                )
            if self.allow_unsigned:
                found.append("GIT_SMS_ALLOW_UNSIGNED is for development only")

        if self.allow_unsigned and self.twilio_auth_token:
            found.append("GIT_SMS_ALLOW_UNSIGNED and TWILIO_AUTH_TOKEN are contradictory; unset one")
        if self.is_development and not self.allow_unsigned and not self.twilio_auth_token:
            found.append(
                "TWILIO_AUTH_TOKEN is required to verify webhooks (development may set GIT_SMS_ALLOW_UNSIGNED=true)"
            )
        if self.audit_retention_days < self.retention_days:
            found.append("GIT_SMS_AUDIT_RETENTION_DAYS must be at least GIT_SMS_RETENTION_DAYS")

        if self.sender_backend == "twilio" and not (
            self.twilio_account_sid
            and (self.twilio_auth_token or (self.twilio_api_key_sid and self.twilio_api_key_secret))
        ):
            found.append("the Twilio sender needs TWILIO_ACCOUNT_SID and an auth token or API key")

        app_fields = (self.github_app_id, self.github_app_private_key, self.github_app_installation_id)
        if any(app_fields) and not all(app_fields):
            found.append("GitHub App auth needs GITHUB_APP_ID, a private key and GITHUB_APP_INSTALLATION_ID")
        if all(app_fields) and self.github_token:
            found.append("configure either GITHUB_TOKEN or a GitHub App, not both")

        if self.retired_llm_settings:
            found.append(
                f"{', '.join(self.retired_llm_settings)}: GitHub Models was retired on 2026-07-30; "
                "use OPENAI_API_KEY or LLM_BASE_URL/LLM_API_KEY, plus LLM_MODEL"
            )
        llm_parts = (self.llm_base_url, self.llm_api_key, self.llm_model)
        if any(llm_parts) and not all(llm_parts):
            found.append("an AI model needs LLM_BASE_URL (or OPENAI_API_KEY), an API key and LLM_MODEL")
        return found

    def validate(self) -> None:
        found = self.problems()
        if found:
            raise ConfigError("; ".join(found))
