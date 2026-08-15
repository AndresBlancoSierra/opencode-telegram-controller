"""VPN capability.

The bot talks to a :class:`VpnManager` interface, never to a specific CLI.
Concrete providers (currently NordVPN) implement it. Country/server tokens
coming from Telegram are resolved through :class:`VpnTarget` and validated
against an allowlist before anything is executed.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import Settings
from ..core.process import CommandError, CommandRunner
from .base import ServiceUnavailableError

_SERVER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")


class VpnError(Exception):
    """Raised when a valid VPN operation fails at runtime."""


def _nordvpn_error(result, fallback: str) -> str:
    """Build a human message from ``nordvpn`` output (resists empty stderr)."""
    stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    if stderr_lines:
        return f"{fallback}: {stderr_lines[-1]}"
    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if stdout_lines:
        return f"{fallback}: {stdout_lines[-1]}"
    return fallback


def parse_nordvpn_settings(text: str) -> dict[str, str]:
    """Return parsed ``nordvpn settings`` options (e.g. ``{"Kill Switch": "enabled"}``)."""
    settings: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip().strip(",")
        if not value:
            continue
        settings[key.strip()] = value
    return settings


def _parse_kill_switch(settings_text: str) -> bool | None:
    value = parse_nordvpn_settings(settings_text).get("Kill Switch")
    if value is None:
        return None
    return value.lower() == "enabled"


def _server_from_hostname(hostname: str | None, current_server: str | None) -> str | None:
    if current_server and current_server.strip():
        return current_server.strip()
    if not hostname:
        return None
    return hostname.split(".", 1)[0] or None


def parse_nordvpn_status(text: str) -> VpnStatus:
    """Parse ``nordvpn status`` output into a :class:`VpnStatus`."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fields[key.strip().lower()] = value.strip()
    status = fields.get("status", "")
    if not status:
        return VpnStatus(connected=False, provider=None, error="nordvpn returned no status")
    return VpnStatus(
        connected=status.lower() == "connected",
        provider="NordVPN",
        server=_server_from_hostname(fields.get("hostname"), fields.get("current server")),
        country=fields.get("country"),
        ip=fields.get("ip"),
    )


@dataclass(frozen=True)
class VpnTarget:
    """A validated VPN destination: a country from the allowlist."""

    country: str

    @property
    def command_token(self) -> str:
        return self.country


@dataclass
class VpnStatus:
    connected: bool
    provider: str | None = None
    server: str | None = None
    country: str | None = None
    ip: str | None = None
    kill_switch: bool | None = None
    error: str | None = None

    @property
    def label(self) -> str:
        if self.error:
            return f"⚠️ {self.error}"
        if self.connected:
            parts = [self.country or self.server or "Connected"]
            return "CONNECTED: " + " / ".join(part for part in parts if part)
        return "DISCONNECTED"


@runtime_checkable
class VpnManager(Protocol):
    """Interface every VPN provider implements."""

    name: str

    def available(self) -> bool: ...

    async def status(self) -> VpnStatus: ...

    async def connect(self, target: VpnTarget) -> VpnStatus: ...

    async def connect_dedicated(self, server: str) -> VpnStatus: ...

    async def reconnect(self) -> VpnStatus: ...

    async def disconnect(self) -> VpnStatus: ...

    async def list_countries(self) -> list[str]: ...

    def resolve_target(self, token: str) -> VpnTarget: ...


class NordVpnProvider:
    """NordVPN implementation of :class:`VpnManager` (``nordvpn`` CLI)."""

    name = "NordVPN"

    def __init__(self, *, settings: Settings, runner: CommandRunner) -> None:
        self.settings = settings
        self.runner = runner

    def available(self) -> bool:
        return self.runner.can_run("nordvpn")

    def resolve_target(self, token: str) -> VpnTarget:
        normalized = token.strip().lower()
        allowed = {c.lower() for c in self.settings.vpn_countries}
        if normalized not in allowed:
            raise ValueError(f"Unknown country {token!r}. Allowed: {', '.join(sorted(allowed))}")
        return VpnTarget(country=normalized)

    def validate_server(self, server: str) -> str:
        if not server:
            raise VpnError("No dedicated VPN server is configured (OTC_VPN_DEDICATED_SERVER)")
        if not _SERVER_PATTERN.fullmatch(server):
            raise VpnError("Invalid dedicated VPN server name")
        return server

    async def status(self) -> VpnStatus:
        result = await self.runner.run(
            ("nordvpn", "status"),
            timeout=self.settings.timeout_vpn_seconds,
            check=False,
        )
        vpn = parse_nordvpn_status(result.stdout)
        if vpn.error:
            return vpn
        try:
            settings_result = await self.runner.run(
                ("nordvpn", "settings"),
                timeout=self.settings.timeout_quick_seconds,
                check=False,
            )
        except CommandError:
            return vpn
        vpn.kill_switch = _parse_kill_switch(settings_result.stdout)
        return vpn

    async def connect(self, target: VpnTarget) -> VpnStatus:
        result = await self.runner.run(
            ("nordvpn", "connect", target.command_token),
            timeout=self.settings.timeout_vpn_seconds,
            check=False,
        )
        if not result.ok:
            raise VpnError(_nordvpn_error(result, f"Failed to connect to {target.command_token!r}"))
        return await self.status()

    async def connect_dedicated(self, server: str) -> VpnStatus:
        self.validate_server(server)
        result = await self.runner.run(
            ("nordvpn", "connect", server),
            timeout=self.settings.timeout_vpn_seconds,
            check=False,
        )
        if not result.ok:
            raise VpnError(_nordvpn_error(result, f"Failed to connect to {server!r}"))
        return await self.status()

    async def reconnect(self) -> VpnStatus:
        """Disconnect and connect again (new random server), like the ``cambiar`` alias."""
        await self.runner.run(
            ("nordvpn", "disconnect"),
            timeout=self.settings.timeout_vpn_seconds,
            check=False,
        )
        await asyncio.sleep(2)
        result = await self.runner.run(
            ("nordvpn", "connect"),
            timeout=self.settings.timeout_vpn_seconds,
            check=False,
        )
        if not result.ok:
            raise VpnError(_nordvpn_error(result, "Failed to reconnect"))
        return await self.status()

    async def disconnect(self) -> VpnStatus:
        result = await self.runner.run(
            ("nordvpn", "disconnect"),
            timeout=self.settings.timeout_vpn_seconds,
            check=False,
        )
        if not result.ok:
            raise VpnError(_nordvpn_error(result, "Failed to disconnect"))
        return await self.status()

    async def list_countries(self) -> list[str]:
        result = await self.runner.run(
            ("nordvpn", "countries"),
            timeout=self.settings.timeout_quick_seconds,
            check=False,
        )
        countries = [line for line in result.stdout.lower().splitlines() if line.strip()]
        return sorted({c for c in countries if c})


def build_vpn_manager(*, settings: Settings, runner: CommandRunner) -> VpnManager:
    """Build the configured/most suitable VPN provider.

    Raises :class:`ServiceUnavailableError` when no configured provider is
    available on the machine (so handlers can reply with a clean message
    instead of crashing).
    """
    provider_name = (settings.vpn_provider or "auto").lower()
    candidates: list[VpnManager] = []
    if provider_name in ("auto", "nordvpn"):
        candidates.append(NordVpnProvider(settings=settings, runner=runner))

    for candidate in candidates:
        if candidate.available():
            return candidate
    if provider_name not in ("auto", "nordvpn"):
        raise ServiceUnavailableError("VPN", f"Unknown provider {settings.vpn_provider!r}")
    raise ServiceUnavailableError("VPN", "No VPN CLI found (nordvpn is not installed)")


def available_vpn_managers(*, settings: Settings, runner: CommandRunner) -> list[VpnManager]:
    """Return every VPN provider available on the machine (for /vpn listing)."""
    providers: list[VpnManager] = [NordVpnProvider(settings=settings, runner=runner)]
    return [p for p in providers if p.available()]
