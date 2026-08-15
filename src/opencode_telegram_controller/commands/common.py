"""Shared helpers for the PC Control command routers."""

from __future__ import annotations

import logging

from aiogram.types import Message

from ..core.permissions import PermissionDenied, permission_for_command
from ..core.process import CommandFailedError, CommandNotFoundError, CommandTimeoutError
from ..services.base import ServiceUnavailableError
from ..services.docker import DockerError
from ..services.vpn import VpnError

logger = logging.getLogger(__name__)


async def check_permission(ctx, message: Message, command: str) -> bool:
    """Reject the message when the user lacks the command's permission."""
    registry = getattr(ctx, "permissions", None)
    if registry is None:
        return True
    key = command if command.startswith("/") else f"/{command}"
    try:
        registry.require(message.from_user.id, permission_for_command(key))
        return True
    except PermissionDenied as exc:
        await message.answer(f"⛔ Permission denied: {exc}")
        return False


async def audit(ctx, message: Message, action: str, *, target: str | None = None) -> None:
    """Record an auditable action for the message's user (best effort)."""
    logger_audit = getattr(ctx, "audit", None)
    if logger_audit is None:
        return
    await logger_audit.record(
        user_id=message.from_user.id,
        action=action,
        target=target,
    )


def format_service_error(capability: str, exc: Exception) -> str:
    """Turn a capability failure into a clean, user-safe message."""
    if isinstance(exc, ServiceUnavailableError):
        return f"❌ {exc.capability} unavailable\n\n{exc.reason}"
    if isinstance(exc, CommandNotFoundError):
        return f"❌ {capability} unavailable\n\nRequired tool is not installed."
    if isinstance(exc, CommandTimeoutError):
        return f"❌ {capability} timed out."
    if isinstance(exc, CommandFailedError):
        detail = (exc.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "command failed"
        return f"❌ {capability}: {tail[:300]}"
    if isinstance(exc, ValueError):
        return f"❌ {capability}: {exc}"
    if isinstance(exc, (VpnError, DockerError)):
        return f"❌ {capability}: {exc}"
    logger.warning("%s failed: %r", capability, exc)
    return f"❌ {capability} failed.\n\nDetails are in the service log."
