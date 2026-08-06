"""Application configuration.

Configuration is loaded from environment variables prefixed with ``OTC_`` and
optionally from a ``.env`` file located in the project root. When running under
systemd the service is expected to provide ``EnvironmentFile`` with the same
variable names, which take precedence over the project ``.env`` file.

All secrets (telegram bot token) must be provided through the environment or
an environment file. They are never stored in the repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _parse_csv_list(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            return [str(x) for x in json.loads(text)]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


class Settings(BaseSettings):
    """Runtime configuration for the controller."""

    model_config = SettingsConfigDict(
        env_prefix="OTC_",
        env_file=(str(PROJECT_ROOT / ".env"),),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram -----------------------------------------------------------
    telegram_bot_token: str = ""
    allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_api_base: str = "https://api.telegram.org"
    telegram_nameservers: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Paths --------------------------------------------------------------
    projects_file: Path = PROJECT_ROOT / "config" / "projects.yaml"
    database_path: Path = (
        Path.home() / ".local" / "share" / "opencode-telegram-controller" / "tasks.db"
    )
    data_dir: Path = Path.home() / ".local" / "share" / "opencode-telegram-controller"

    # OpenCode -----------------------------------------------------------
    opencode_bin: str = "opencode"
    opencode_model: str | None = None
    opencode_agent: str = "build"
    opencode_extra_args: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Tasks --------------------------------------------------------------
    default_timeout_seconds: int = 3600
    max_concurrent_tasks: int = 1
    progress_interval_seconds: int = 300
    prompt_max_length: int = 4000

    default_project: str | None = None

    # Summaries ----------------------------------------------------------
    summary_engine: str = "deterministic"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    # Logging ------------------------------------------------------------
    log_level: str = "INFO"
    log_file: Path | None = None

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _validate_user_ids(cls, value: object) -> list[int]:
        parts = _parse_csv_list(value)
        return [int(x) for x in parts]

    @field_validator("telegram_nameservers", mode="before")
    @classmethod
    def _validate_nameservers(cls, value: object) -> list[str]:
        return _parse_csv_list(value)

    @field_validator("opencode_extra_args", mode="before")
    @classmethod
    def _validate_extra_args(cls, value: object) -> list[str]:
        return _parse_csv_list(value)


def load_settings() -> Settings:
    """Load settings, raising a helpful error when required values are missing."""
    settings = Settings()
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "OTC_TELEGRAM_BOT_TOKEN is not set. Configure it in the environment, "
            "a .env file, or the systemd EnvironmentFile."
        )
    if not settings.allowed_user_ids:
        raise RuntimeError(
            "OTC_ALLOWED_USER_IDS is not set. Provide at least one Telegram user ID "
            "allowed to control the bot."
        )
    if settings.max_concurrent_tasks < 1:
        raise RuntimeError("OTC_MAX_CONCURRENT_TASKS must be >= 1")
    if settings.summary_engine not in ("deterministic", "ollama"):
        raise RuntimeError(
            f"OTC_SUMMARY_ENGINE={settings.summary_engine!r} is not supported; "
            "use 'deterministic' or 'ollama'"
        )
    return settings
