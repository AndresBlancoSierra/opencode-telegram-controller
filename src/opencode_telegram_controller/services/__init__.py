"""Service layer for the PC Control capabilities.

Each service encapsulates a validated, semantic operation. Services never
accept arbitrary strings to build commands; they construct fixed ``argv``
lists from validated inputs and run them through :class:`CommandRunner`
without a shell.
"""

from __future__ import annotations

from .desktop import DesktopManager
from .docker import DockerManager
from .media import MediaManager
from .monitoring import HealthMonitor
from .network import NetworkManager
from .power import PowerManager
from .stream import LiveStreamManager
from .system import SystemManager
from .vpn import (
    NordVpnProvider,
    VpnManager,
    VpnTarget,
    available_vpn_managers,
    build_vpn_manager,
)

__all__ = [
    "DesktopManager",
    "DockerManager",
    "HealthMonitor",
    "LiveStreamManager",
    "MediaManager",
    "NetworkManager",
    "NordVpnProvider",
    "PowerManager",
    "SystemManager",
    "VpnManager",
    "VpnTarget",
    "available_vpn_managers",
    "build_vpn_manager",
]
