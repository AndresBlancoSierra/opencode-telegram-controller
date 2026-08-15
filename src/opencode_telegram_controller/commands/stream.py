"""Stream commands: /stream and /stream_stop.

Handlers are thin and registered onto the bot's router by :func:`register`.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from .common import audit, check_permission


async def on_stream(message: Message, ctx) -> None:
    """Start sending live screen clips to this chat."""
    logger.info("/stream from user {}", message.from_user.id)
    if not await check_permission(ctx, message, "stream"):
        return
    stream = getattr(ctx, "stream", None)
    if stream is None:
        await message.answer("❌ Stream capability is unavailable.")
        return
    try:
        await stream.start(message.chat.id)
    except Exception as exc:
        logger.warning("/stream failed: {!r}", exc)
        await message.answer(str(exc))
        return
    await audit(ctx, message, "stream.start")
    await message.answer(
        "📡 Live stream started. Video clips are sent every few seconds.\n"
        "Send /stream_stop to end it."
    )


async def on_stream_stop(message: Message, ctx) -> None:
    """Stop the live stream in this chat."""
    logger.info("/stream_stop from user {}", message.from_user.id)
    if not await check_permission(ctx, message, "stream"):
        return
    stream = getattr(ctx, "stream", None)
    if stream is None:
        await message.answer("❌ Stream capability is unavailable.")
        return
    try:
        stopped = await stream.stop(message.chat.id)
    except Exception as exc:
        await message.answer(str(exc))
        return
    if not stopped:
        await message.answer("No live stream is active in this chat.")
        return
    await audit(ctx, message, "stream.stop")
    await message.answer("🛑 Live stream stopped.")


def register(router: Router) -> None:
    """Register this command group onto an existing router."""
    router.message.register(on_stream, Command("stream"))
    router.message.register(on_stream_stop, Command("stream_stop"))
