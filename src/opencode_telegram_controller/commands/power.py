"""Power commands: /reboot /shutdown /sleep (+ /confirm_* and /dismiss).

Destructive actions never run on the first message. They register a pending
confirmation and only execute when the user replies with the matching
``/confirm_<action>`` command while it is still valid.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..core.confirmation import NoPendingConfirmation
from ..services.power import POWER_ACTIONS, PowerManager
from .common import audit, check_permission, format_service_error

_ACTION_LABELS = {
    "reboot": "reboot",
    "shutdown": "shut down",
    "sleep": "suspend",
}


def _make_request_handler(action: str):
    async def handler(message: Message, ctx) -> None:
        if not await check_permission(ctx, message, action):
            return
        power: PowerManager | None = getattr(ctx, "power", None)
        if power is None:
            await message.answer("❌ Power capability is unavailable.")
            return
        confirmations = getattr(ctx, "confirmations", None)
        if confirmations is None:
            await message.answer("❌ Confirmation support is not available.")
            return
        timeout = (
            ctx.settings.power_confirmation_timeout_seconds
            if getattr(ctx, "settings", None)
            else 60.0
        )
        confirmations.request(
            message.from_user.id,
            action,
            params={"command": f"/{action}"},
            timeout_seconds=timeout,
        )
        label = _ACTION_LABELS[action]
        await message.answer(
            f"⚠️ <b>{label.title()} requested.</b>\n"
            f"Confirm with <code>/confirm_{action}</code> within <b>{int(timeout)}s</b> "
            f"or cancel with <code>/dismiss</code>."
        )

    return handler


def _make_confirm_handler(action: str):
    async def handler(message: Message, ctx) -> None:
        if not await check_permission(ctx, message, f"confirm_{action}"):
            return
        power: PowerManager | None = getattr(ctx, "power", None)
        confirmations = getattr(ctx, "confirmations", None)
        if power is None or confirmations is None:
            await message.answer("❌ Power capability is unavailable.")
            return
        try:
            confirmation = confirmations.confirm(message.from_user.id, action)
        except NoPendingConfirmation as exc:
            await message.answer(f"❗ {exc}\n\nUse <code>/{action}</code> first to request one.")
            return
        try:
            result = await power.perform(confirmation)
        except Exception as exc:
            await message.answer(format_service_error(_ACTION_LABELS[action].title(), exc))
            return
        await audit(ctx, message, f"power.{action}")
        await message.answer(f"✅ {result.detail} executed.")

    return handler


async def on_dismiss(message: Message, ctx) -> None:
    """Cancel every pending confirmation of the user (no action executes)."""
    if not await check_permission(ctx, message, "dismiss"):
        return
    confirmations = getattr(ctx, "confirmations", None)
    if confirmations is None:
        await message.answer("❌ Confirmation support is not available.")
        return
    pending = confirmations.pending_for_user(message.from_user.id)
    for confirmation in pending:
        confirmations.dismiss(message.from_user.id, confirmation.action)
    if not pending:
        await message.answer("No pending confirmations to cancel.")
        return
    await audit(ctx, message, "power.dismiss", target=",".join(c.action for c in pending))
    await message.answer(f"✅ Cancelled {len(pending)} pending confirmation(s).")


def register(router: Router) -> None:
    """Register this command group onto an existing router."""
    for action in POWER_ACTIONS:
        router.message.register(_make_request_handler(action), Command(action))
        router.message.register(_make_confirm_handler(action), Command(f"confirm_{action}"))
    router.message.register(on_dismiss, Command("dismiss"))
