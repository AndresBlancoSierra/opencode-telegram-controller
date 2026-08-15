"""Permission model for PC capabilities.

Three levels are defined:

* READ — read-only queries (status, resources, docker status, ...)
* CONTROL — mutating but reversible operations (vpn connect, docker restart)
* DESTRUCTIVE — irreversible or impactful actions (reboot, shutdown)

By default every allowlisted Telegram user is an admin (full access). A
read-only set can be configured so some users can only inspect the machine.
"""

from __future__ import annotations

import enum


class Permission(enum.StrEnum):
    READ = "READ"
    CONTROL = "CONTROL"
    DESTRUCTIVE = "DESTRUCTIVE"


_PERCENTAGE_RANK = {
    Permission.READ: 0,
    Permission.CONTROL: 1,
    Permission.DESTRUCTIVE: 2,
}


class PermissionDenied(Exception):
    """Raised when a user lacks the permission required by an action."""


class PermissionRegistry:
    """Resolves which permission level a user is granted."""

    def __init__(
        self,
        *,
        admin_user_ids: list[int] | set[int],
        read_only_user_ids: list[int] | set[int] | None = None,
    ) -> None:
        self._admins = set(admin_user_ids)
        self._read_only = set(read_only_user_ids or [])

    def level_for(self, user_id: int) -> Permission:
        if user_id in self._admins and user_id not in self._read_only:
            return Permission.DESTRUCTIVE
        if user_id in self._read_only:
            return Permission.READ
        return Permission.READ

    def require(self, user_id: int, required: Permission) -> None:
        """Raise :class:`PermissionDenied` when the user's level is too low."""
        if _PERCENTAGE_RANK[self.level_for(user_id)] < _PERCENTAGE_RANK[required]:
            raise PermissionDenied(f"This action requires {required.value} permission")

    def can(self, user_id: int, required: Permission) -> bool:
        return _PERCENTAGE_RANK[self.level_for(user_id)] >= _PERCENTAGE_RANK[required]


def permission_for_command(command: str) -> Permission:
    """Return the permission level a built-in command requires.

    Unknown commands default to :class:`Permission.READ`; the router resolves
    real commands against ``COMMAND_PERMISSIONS``.
    """
    return COMMAND_PERMISSIONS.get(command, Permission.READ)


COMMAND_PERMISSIONS: dict[str, Permission] = {
    # OpenCode (preserved behaviour)
    "/status": Permission.READ,
    "/start": Permission.READ,
    "/help": Permission.READ,
    "/projects": Permission.READ,
    "/use": Permission.CONTROL,
    "/tasks": Permission.READ,
    "/new": Permission.CONTROL,
    "/history": Permission.READ,
    "/continue": Permission.CONTROL,
    "/current": Permission.READ,
    "/task": Permission.READ,
    "/cancel": Permission.CONTROL,
    "/logs": Permission.READ,
    # System
    "/resources": Permission.READ,
    "/disk": Permission.READ,
    "/processes": Permission.READ,
    "/health": Permission.READ,
    # Network
    "/ip": Permission.READ,
    "/dns": Permission.READ,
    "/network": Permission.READ,
    "/vpn": Permission.CONTROL,
    "/vpn_status": Permission.READ,
    "/vpn_dedicated": Permission.CONTROL,
    "/vpn_change": Permission.CONTROL,
    "/cambiar": Permission.CONTROL,
    # Docker
    "/docker": Permission.READ,
    "/docker_status": Permission.READ,
    "/docker_restart": Permission.CONTROL,
    "/docker_logs": Permission.READ,
    # Desktop
    "/screenshot": Permission.CONTROL,
    "/windows": Permission.READ,
    "/lock": Permission.CONTROL,
    # Media
    "/playback": Permission.CONTROL,
    "/photo": Permission.CONTROL,
    "/record_mic": Permission.CONTROL,
    "/stream": Permission.CONTROL,
    "/stream_stop": Permission.CONTROL,
    # Power
    "/reboot": Permission.DESTRUCTIVE,
    "/shutdown": Permission.DESTRUCTIVE,
    "/sleep": Permission.DESTRUCTIVE,
    "/confirm_reboot": Permission.DESTRUCTIVE,
    "/confirm_shutdown": Permission.DESTRUCTIVE,
    "/confirm_sleep": Permission.DESTRUCTIVE,
    "/dismiss": Permission.CONTROL,
}
