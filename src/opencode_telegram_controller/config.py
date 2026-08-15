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
    read_only_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
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

    # PC Control: timeouts (seconds) -------------------------------------
    timeout_quick_seconds: int = 10
    timeout_docker_seconds: int = 10
    timeout_vpn_seconds: int = 60

    # PC Control: VPN -----------------------------------------------------
    vpn_provider: str = "auto"
    vpn_dedicated_server: str | None = None
    vpn_countries: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # PC Control: Docker --------------------------------------------------
    docker_socket: str = "unix:///var/run/docker.sock"
    docker_allowed_containers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    docker_logs_lines: int = 200

    # PC Control: Desktop -------------------------------------------------
    screenshot_enabled: bool = True

    # PC Control: Media (camera, mic, playback, stream) -------------------
    camera_device: str = "/dev/video0"
    camera_resolution: str = "1280x720"
    mic_source: str = "default"
    mic_default_seconds: int = 10
    mic_max_seconds: int = 120
    media_max_download_mb: int = 20
    playback_max_seconds: int = 3600
    stream_clip_seconds: float = 5.0
    stream_framerate: int = 15
    stream_with_audio: bool = True

    # PC Control: Power ---------------------------------------------------
    power_confirmation_timeout_seconds: int = 60
    power_reboot_command: Annotated[list[str], NoDecode] = Field(default_factory=list)
    power_shutdown_command: Annotated[list[str], NoDecode] = Field(default_factory=list)
    power_sleep_command: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # PC Control: health monitoring (0 = disabled) -------------------------
    health_check_interval_seconds: int = 0

    # Logging ------------------------------------------------------------
    log_level: str = "INFO"
    log_file: Path | None = None

    @field_validator("allowed_user_ids", "read_only_user_ids", mode="before")
    @classmethod
    def _validate_user_ids(cls, value: object) -> list[int]:
        parts = _parse_csv_list(value)
        return [int(x) for x in parts]

    @field_validator(
        "telegram_nameservers",
        "vpn_countries",
        "docker_allowed_containers",
        "power_reboot_command",
        "power_shutdown_command",
        "power_sleep_command",
        mode="before",
    )
    @classmethod
    def _validate_str_list(cls, value: object) -> list[str]:
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
    if settings.vpn_provider not in ("auto", "nordvpn", "none"):
        raise RuntimeError(
            f"OTC_VPN_PROVIDER={settings.vpn_provider!r} is not supported; "
            "use 'auto', 'nordvpn' or 'none'"
        )
    if settings.power_confirmation_timeout_seconds <= 0:
        raise RuntimeError("OTC_POWER_CONFIRMATION_TIMEOUT must be >= 1")
    if settings.health_check_interval_seconds < 0:
        raise RuntimeError("OTC_HEALTH_CHECK_INTERVAL_SECONDS must be >= 0 (0 disables)")
    if not settings.camera_resolution.strip():
        raise RuntimeError("OTC_CAMERA_RESOLUTION must not be empty")
    if settings.mic_default_seconds < 1:
        raise RuntimeError("OTC_MIC_DEFAULT_SECONDS must be >= 1")
    if settings.mic_max_seconds < settings.mic_default_seconds:
        raise RuntimeError("OTC_MIC_MAX_SECONDS must be >= OTC_MIC_DEFAULT_SECONDS")
    if settings.media_max_download_mb < 1:
        raise RuntimeError("OTC_MEDIA_MAX_DOWNLOAD_MB must be >= 1")
    if settings.playback_max_seconds < 1:
        raise RuntimeError("OTC_PLAYBACK_MAX_SECONDS must be >= 1")
    if settings.stream_clip_seconds < 1:
        raise RuntimeError("OTC_STREAM_CLIP_SECONDS must be >= 1")
    return settings
