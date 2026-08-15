"""Desktop commands: /screenshot /windows /lock.

Handlers are thin and registered onto the bot's router by :func:`register`.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from .common import audit, check_permission, format_service_error


async def on_screenshot(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "screenshot"):
        return
    desktop = getattr(ctx, "desktop", None)
    if desktop is None:
        await message.answer("❌ Desktop capability is unavailable.")
        return
    try:
        shot = await desktop.screenshot()
    except Exception as exc:
        await message.answer(format_service_error("Screenshot", exc))
        return
    await audit(ctx, message, "desktop.screenshot")
    await message.answer_photo(
        FSInputFile(shot.path, filename="screenshot.png"),
        caption="📸 Screenshot",
    )


async def on_windows(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "windows"):
        return
    desktop = getattr(ctx, "desktop", None)
    if desktop is None:
        await message.answer("❌ Desktop capability is unavailable.")
        return
    try:
        windows = await desktop.windows(limit=20)
    except Exception as exc:
        await message.answer(format_service_error("Windows", exc))
        return
    lines = ["🪟 <b>WINDOWS</b>"]
    if not windows:
        lines.append("No visible windows.")
    for window in windows:
        ws = f"ws{window.workspace}" if window.workspace is not None else "-"
        cls = window.class_name or "-"
        lines.append(f"· {ws} | <code>{window.title[:40]}</code> ({cls})")
    await message.answer("\n".join(lines))


async def on_lock(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "lock"):
        return
    desktop = getattr(ctx, "desktop", None)
    if desktop is None:
        await message.answer("❌ Desktop capability is unavailable.")
        return
    try:
        await desktop.lock()
    except Exception as exc:
        await message.answer(format_service_error("Lock", exc))
        return
    await audit(ctx, message, "desktop.lock")
    await message.answer("🔒 Screen locked.")


def register(router: Router) -> None:
    """Register this command group onto an existing router."""
    router.message.register(on_screenshot, Command("screenshot"))
    router.message.register(on_windows, Command("windows"))
    router.message.register(on_lock, Command("lock"))
