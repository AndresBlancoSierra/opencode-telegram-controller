"""Media commands: auto-playback of sent files, /photo and /record_mic.

Audio files play in the background with no visible window; video files open
fullscreen and close when playback ends. /photo captures the camera and
/record_mic records the microphone to MP3. Handlers are thin and registered
onto the bot's router by :func:`register`.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from .common import audit, check_permission, format_service_error


def _media(ctx) -> object | None:
    return getattr(ctx, "media", None)


async def on_audio(message: Message, ctx) -> None:
    """Auto-play a sent audio file in the background."""
    await _play_audio_message(message, ctx, message.audio.file_id, suffix=".mp3")


async def on_voice(message: Message, ctx) -> None:
    """Auto-play a sent voice note in the background."""
    await _play_audio_message(message, ctx, message.voice.file_id, suffix=".ogg")


async def _play_audio_message(message: Message, ctx, file_id: str, *, suffix: str) -> None:
    if not await check_permission(ctx, message, "playback"):
        return
    media = _media(ctx)
    if media is None:
        await message.answer("❌ Media capability is unavailable.")
        return
    try:
        path = await media.download_to_temp(message.bot, file_id, suffix=suffix)
        await media.play_audio(path)
    except Exception as exc:
        await message.answer(format_service_error("Playback", exc))
        return
    await audit(ctx, message, "media.play_audio")
    await message.answer("🎵 Reproduciendo audio en el equipo…")


async def on_video(message: Message, ctx) -> None:
    """Auto-play a sent video file fullscreen."""
    if not await check_permission(ctx, message, "playback"):
        return
    media = _media(ctx)
    if media is None:
        await message.answer("❌ Media capability is unavailable.")
        return
    try:
        path = await media.download_to_temp(message.bot, message.video.file_id, suffix=".mp4")
        await media.play_video(path)
    except Exception as exc:
        await message.answer(format_service_error("Playback", exc))
        return
    await audit(ctx, message, "media.play_video")
    await message.answer("🎬 Reproduciendo video a pantalla completa…")


async def on_document(message: Message, ctx) -> None:
    """Auto-play sent documents whose mime type is audio or video."""
    mime = (message.document.mime_type or "").lower()
    if mime.startswith("video/"):
        return await on_video(message, ctx)
    if mime.startswith("audio/"):
        return await on_audio(message, ctx)


async def on_photo(message: Message, ctx) -> None:
    """Capture a photo from the camera."""
    if not await check_permission(ctx, message, "photo"):
        return
    media = _media(ctx)
    if media is None:
        await message.answer("❌ Media capability is unavailable.")
        return
    try:
        result = await media.photo()
    except Exception as exc:
        await message.answer(format_service_error("Camera", exc))
        return
    await audit(ctx, message, "media.photo")
    await message.answer_photo(
        FSInputFile(result.path, filename="photo.jpg"),
        caption="📷 Foto de la cámara",
    )


async def on_record_mic(message: Message, ctx) -> None:
    """Record the microphone for the given number of seconds."""
    if not await check_permission(ctx, message, "record_mic"):
        return
    media = _media(ctx)
    if media is None:
        await message.answer("❌ Media capability is unavailable.")
        return
    settings = getattr(ctx, "settings", None)
    seconds = settings.mic_default_seconds if settings else 10
    parts = (message.text or "").split()
    if len(parts) > 1:
        try:
            seconds = int(parts[1])
        except ValueError:
            await message.answer("Usage: /record_mic [seconds]")
            return
    max_seconds = settings.mic_max_seconds if settings else 120
    if not 1 <= seconds <= max_seconds:
        await message.answer(f"Recording length must be between 1 and {max_seconds} seconds.")
        return
    await message.answer(f"🎙️ Grabando micrófono ({seconds}s)…")
    try:
        result = await media.record_mic(seconds)
    except Exception as exc:
        await message.answer(format_service_error("Microphone", exc))
        return
    await audit(ctx, message, "media.record_mic", target=f"{seconds}s")
    await message.answer_audio(
        FSInputFile(result.path, filename="record.mp3"),
        caption="🎙️ Grabación de micrófono",
    )


def register(router: Router) -> None:
    """Register this command group onto an existing router."""
    router.message.register(on_audio, F.audio)
    router.message.register(on_voice, F.voice)
    router.message.register(on_video, F.video)
    router.message.register(on_document, F.document)
    router.message.register(on_photo, Command("photo"))
    router.message.register(on_record_mic, Command("record_mic"))
