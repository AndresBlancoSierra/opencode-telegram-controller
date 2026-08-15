"""Network capability: public/local addresses, DNS and interface summary."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field

import httpx

from ..config import Settings
from ..core.process import CommandError, CommandRunner

_IP_PROVIDERS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


@dataclass
class InterfaceInfo:
    name: str
    state: str
    ipv4: str | None = None
    gateway: str | None = None


@dataclass
class NetworkInfo:
    interfaces: list[InterfaceInfo] = field(default_factory=list)

    @property
    def active_interfaces(self) -> list[InterfaceInfo]:
        return [i for i in self.interfaces if i.state == "up"]


@dataclass
class DnsSnapshot:
    backend: str  # systemd-resolved | networkmanager | plain
    servers: list[str] = field(default_factory=list)
    search_domains: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class NetworkStatus:
    """Snapshot for /ip: public/local addresses and active interface."""

    public_ip: str | None
    local_ip: str | None
    interface: str | None
    gateway: str | None = None

    @property
    def online(self) -> bool:
        return bool(self.public_ip)


def _hex_to_ipv4(hex_addr: str) -> str:
    try:
        packed = bytes.fromhex(hex_addr)
        return socket.inet_ntoa(packed[::-1])
    except (ValueError, OSError):
        return ""


def parse_route_table(text: str) -> str | None:
    """Extract the default gateway IPv4 from /proc/net/route.

    Entries are hex little-endian; the default route has Dest ``00000000``.
    """
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        destination = parts[1]
        if destination == "00000000":
            gateway = _hex_to_ipv4(parts[2])
            if gateway:
                return gateway
    return None


def parse_resolv_conf(text: str) -> tuple[list[str], list[str]]:
    servers: list[str] = []
    domains: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("nameserver"):
            tokens = stripped.split()
            if len(tokens) >= 2:
                servers.append(tokens[1])
        elif stripped.startswith("search"):
            domains.extend(stripped.split()[1:])
        elif stripped.startswith("domain"):
            tokens = stripped.split()
            if len(tokens) >= 2:
                domains.append(tokens[1])
    return servers, domains


class NetworkManager:
    """Read-only inspection of the machine's network state.

    VPN connectivity is delegated to the configured ``VpnManager``. Everything
    here is a query; nothing mutates networking.
    """

    def __init__(self, *, settings: Settings, runner: CommandRunner) -> None:
        self.settings = settings
        self.runner = runner

    def available(self) -> bool:
        return True

    async def _read(self, path: str) -> str:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()

    async def public_ip(self) -> str | None:
        """Resolve the public IP through one of the fixed providers."""
        async with httpx.AsyncClient(timeout=self.settings.timeout_quick_seconds) as client:
            for provider in _IP_PROVIDERS:
                try:
                    response = await client.get(provider)
                    if response.status_code == 200 and response.text.strip():
                        return response.text.strip()
                except httpx.HTTPError:
                    continue
        return None

    async def gateway_configured(self) -> bool:
        route = await self._read("/proc/net/route")
        return parse_route_table(route) is not None

    async def gateway(self) -> str | None:
        route = await self._read("/proc/net/route")
        return parse_route_table(route)

    async def _interfaces_via_ip(self) -> NetworkInfo:
        result = await self.runner.run(
            ("ip", "-j", "addr", "show"),
            timeout=self.settings.timeout_quick_seconds,
        )
        data = json.loads(result.stdout)
        infos: list[InterfaceInfo] = []
        for entry in data:
            name = entry.get("ifname", "")
            if name == "lo":
                continue
            operstate = entry.get("operstate", "up")
            ipv4 = None
            for addr in entry.get("addr_info", []):
                if addr.get("family") == "inet" and addr.get("scope") == "global":
                    ipv4 = addr.get("local")
                    break
            infos.append(InterfaceInfo(name=name, state=operstate, ipv4=ipv4))
        return NetworkInfo(interfaces=infos)

    async def _interfaces_fallback(self) -> NetworkInfo:
        infos: list[InterfaceInfo] = []
        for ifname in _list_network_interfaces():
            if ifname == "lo":
                continue
            state = _sys_interface_state(ifname)
            infos.append(InterfaceInfo(name=ifname, state=state))
        return NetworkInfo(interfaces=infos)

    async def network_info(self) -> NetworkInfo:
        if self.runner.can_run("ip"):
            try:
                return await self._interfaces_via_ip()
            except (CommandError, json.JSONDecodeError):
                pass
        return await self._interfaces_fallback()

    async def dns(self) -> DnsSnapshot:
        resolv_path = "/etc/resolv.conf"
        try:
            resolv = await self._read(resolv_path)
        except OSError:
            resolv = ""
        servers, domains = parse_resolv_conf(resolv)
        notes: list[str] = []

        backend = "plain"
        if self.runner.can_run("resolvectl"):
            backend = "systemd-resolved"
            notes.append("systemd-resolved in use (resolvectl available)")
        elif "127.0.0.53" in servers:
            backend = "systemd-resolved"
            notes.append("resolver forwards through systemd-resolved (127.0.0.53)")

        if self.runner.can_run("nmcli"):
            notes.append("NetworkManager detects DNS (nmcli available)")

        if any(server.startswith("10.") or server.startswith("192.168.") for server in servers):
            notes.append("local/private resolver configured")

        return DnsSnapshot(
            backend=backend,
            servers=servers,
            search_domains=domains,
            notes=notes,
        )

    async def status(self) -> NetworkStatus:
        info = await self.network_info()
        active = info.active_interfaces[:1]
        local_ip = active[0].ipv4 if active else None
        interface = active[0].name if active else None
        return NetworkStatus(
            public_ip=await self.public_ip(),
            local_ip=local_ip,
            interface=interface,
            gateway=await self.gateway(),
        )


def _list_network_interfaces() -> list[str]:
    """Return interface names present under /sys/class/net."""
    try:
        return sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []


def _sys_interface_state(ifname: str) -> str:
    path = f"/sys/class/net/{ifname}/operstate"
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip() or "up"
    except OSError:
        return "up"
