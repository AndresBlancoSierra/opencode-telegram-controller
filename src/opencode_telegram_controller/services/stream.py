"""Live stream capability: short video clips of the screen.

The Telegram Bot API cannot stream in real time, so the closest option is a
series of short ``wf-recorder`` clips (default 5s, with microphone audio) sent
as video messages while the stream is active. ``wf-recorder`` is the standard
wlroots screen recorder and works with Hyprland.

One stream per chat; ``/stream_stop`` (or a loop error) stops it cleanly.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from loguru import logger

from ..config import Settings
from ..core.process import CommandRunner, signal_group
from .base import ServiceUnavailableError

_STREAM_DIR_NAME = "streams"

_STOP_GRACE_SECONDS = 10.0


class StreamError(Exception):
    """A user-facing live-stream error."""


@dataclass
class _Stream:
    chat_id: int
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class LiveStreamManager:
    """Records the screen as short clips and sends them to a chat."""

    def __init__(self, *, settings: Settings, runner: CommandRunner, bot) -> None:
        self.settings = settings
        self.runner = runner
        self._bot = bot
        self._stream_dir = settings.data_dir / _STREAM_DIR_NAME
        self._streams: dict[int, _Stream] = {}

    def available(self) -> bool:
        return self.runner.can_run("wf-recorder")

    def is_streaming(self, chat_id: int) -> bool:
        return chat_id in self._streams

    def active_chats(self) -> list[int]:
        return list(self._streams)

    async def start(self, chat_id: int) -> None:
        """Start streaming to ``chat_id`` (raises :class:`StreamError`)."""
        if chat_id in self._streams:
            raise StreamError("A live stream is already running in this chat.")
        if not self.available():
            raise StreamError(
                "wf-recorder is not installed. Install it with: pacman -S wf-recorder"
            )
        stream = _Stream(chat_id=chat_id)
        self._streams[chat_id] = stream
        stream.task = asyncio.get_running_loop().create_task(self._loop(stream))
        stream.task.add_done_callback(lambda _task: self._streams.pop(chat_id, None))

    async def stop(self, chat_id: int) -> bool:
        """Stop streaming to ``chat_id``. Returns whether a stream was active."""
        stream = self._streams.get(chat_id)
        if stream is None:
            return False
        stream.stop.set()
        if stream.task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await stream.task
        return True

    async def stop_all(self) -> None:
        """Stop every active stream (used on shutdown)."""
        for chat_id in list(self._streams):
            await self.stop(chat_id)

    async def _loop(self, stream: _Stream) -> None:
        chat_id = stream.chat_id
        logger.info("Stream loop started for chat {}", chat_id)
        try:
            while not stream.stop.is_set():
                path = await self._record_clip(stream)
                if path is None:
                    break
                try:
                    await self._bot.send_video(
                        chat_id,
                        path,
                        caption=f"📡 Live — {_timestamp()}",
                    )
                finally:
                    path.unlink(missing_ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Live stream for chat {} failed", chat_id)
            with suppress(Exception):
                await self._bot.send_message(chat_id, f"❌ Stream stopped: {exc}")
        finally:
            stream.stop.set()

    async def _record_clip(self, stream: _Stream) -> Path | None:
        """Record one clip, stopping on ``stream.stop``. Returns None if stopped."""
        self._stream_dir.mkdir(parents=True, exist_ok=True)
        target = self._stream_dir / f"stream-{uuid4().hex}.mp4"
        with_audio = self.settings.stream_with_audio
        for attempt in range(2):
            if stream.stop.is_set():
                return None
            try:
                await self._record_once(target, with_audio=with_audio, stop=stream.stop)
                if not target.exists() or target.stat().st_size == 0:
                    target.unlink(missing_ok=True)
                    raise ServiceUnavailableError("Stream", "recording produced an empty clip")
                return target
            except ServiceUnavailableError:
                target.unlink(missing_ok=True)
                if with_audio and attempt == 0:
                    logger.warning("Stream clip failed with audio; retrying without audio")
                    with_audio = False
                    continue
                raise
        return None

    async def _record_once(self, target: Path, *, with_audio: bool, stop: asyncio.Event) -> None:
        command = [
            "wf-recorder",
            "-r",
            str(self.settings.stream_framerate),
            "-c",
            "libx264",
            "-p",
            "preset=ultrafast",
            "-p",
            "crf=28",
        ]
        if with_audio:
            command.append("-a")
        command += ["-f", str(target)]
        proc = await self.runner.spawn(command)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=self.settings.stream_clip_seconds)
        await signal_group(proc, signal.SIGINT)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
        except TimeoutError:
            await signal_group(proc, signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
        if proc.returncode is None:
            await signal_group(proc, signal.SIGKILL)
            with suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
        if proc.returncode != 0:
            detail = f"wf-recorder exited with code {proc.returncode}"
            raise ServiceUnavailableError("Stream", detail)


def _timestamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%H:%M:%S UTC")
