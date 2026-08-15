"""Media capability: playback, camera photos and microphone recording.

The bot downloads audio/video files sent by the user and plays them locally:
audio plays in the background with no visible window (``mpv --no-video``),
video opens fullscreen and closes when it finishes (``mpv --fullscreen
--keep-open=no``). Camera photos use ffmpeg/v4l2; microphone recording uses
ffmpeg/pulse. Every command is a fixed argv list executed without a shell.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from loguru import logger

from ..config import Settings
from ..core.process import CommandFailedError, CommandRunner, signal_group
from .base import ServiceUnavailableError

_MEDIA_DIR_NAME = "media"


@dataclass
class PhotoResult:
    path: Path


@dataclass
class RecordingResult:
    path: Path


class MediaManager:
    """Plays media and captures camera / microphone input."""

    def __init__(self, *, settings: Settings, runner: CommandRunner) -> None:
        self.settings = settings
        self.runner = runner
        self._media_dir = settings.data_dir / _MEDIA_DIR_NAME
        self._playback_tasks: set[asyncio.Task] = set()

    # --- playback --------------------------------------------------------

    def can_play(self) -> bool:
        return self.runner.can_run("mpv")

    async def download_to_temp(self, bot, file_id: str, suffix: str) -> Path:
        """Download a Telegram file (bounded size) to a temporary path."""
        self._media_dir.mkdir(parents=True, exist_ok=True)
        file_info = await bot.get_file(file_id)
        limit_bytes = self.settings.media_max_download_mb * 1024 * 1024
        if file_info.file_size and file_info.file_size > limit_bytes:
            raise ServiceUnavailableError(
                "Media",
                f"File is too large (>{self.settings.media_max_download_mb} MB limit)",
            )
        if not file_info.file_path:
            raise ServiceUnavailableError("Media", "Could not resolve the file path")
        target = self._media_dir / f"play-{uuid4().hex}{suffix}"
        await bot.download_file(file_info.file_path, destination=target)
        return target

    async def play_audio(self, path: Path) -> None:
        """Play an audio file in the background (no visible window)."""
        self._require("Playback", "mpv")
        proc = await self.runner.spawn(
            ("mpv", "--no-video", "--audio-display=no", "--keep-open=no", str(path))
        )
        self._schedule_supervisor(proc, path, "audio")

    async def play_video(self, path: Path) -> None:
        """Play a video fullscreen; the window closes when playback ends."""
        self._require("Playback", "mpv")
        proc = await self.runner.spawn(
            ("mpv", "--fullscreen", "--fs-screen=current", "--ontop", "--keep-open=no", str(path))
        )
        self._schedule_supervisor(proc, path, "video")

    def _schedule_supervisor(self, proc: asyncio.subprocess.Process, path: Path, kind: str) -> None:
        task = asyncio.get_running_loop().create_task(self._supervise_playback(proc, path, kind))
        self._playback_tasks.add(task)
        task.add_done_callback(self._playback_tasks.discard)

    async def _supervise_playback(
        self, proc: asyncio.subprocess.Process, path: Path, kind: str
    ) -> None:
        limit = self.settings.playback_max_seconds
        try:
            await asyncio.wait_for(proc.wait(), timeout=limit)
        except TimeoutError:
            logger.warning("Playback ({}) exceeded {}s, killing mpv", kind, limit)
            await signal_group(proc, signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2)
            if proc.returncode is None:
                await signal_group(proc, signal.SIGKILL)
                with suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2)
        else:
            if proc.returncode not in (0, None):
                logger.warning("Playback ({}) exited with code {}", kind, proc.returncode)
        finally:
            path.unlink(missing_ok=True)

    # --- camera ----------------------------------------------------------

    def camera_available(self) -> bool:
        return Path(self.settings.camera_device).exists()

    async def photo(self) -> PhotoResult:
        """Capture one photo from the camera via ffmpeg/v4l2."""
        self._require("Camera", "ffmpeg")
        if not self.camera_available():
            raise ServiceUnavailableError(
                "Camera", f"camera device {self.settings.camera_device} is not present"
            )
        self._media_dir.mkdir(parents=True, exist_ok=True)
        target = self._media_dir / f"photo-{uuid4().hex}.jpg"
        command = (
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-input_format",
            "mjpeg",
            "-video_size",
            self.settings.camera_resolution,
            "-i",
            self.settings.camera_device,
            "-frames:v",
            "1",
            "-y",
            str(target),
        )
        try:
            await self.runner.run(command, timeout=self.settings.timeout_quick_seconds * 2)
        except CommandFailedError as exc:
            target.unlink(missing_ok=True)
            detail = (exc.stderr or "").strip().splitlines()
            tail = detail[-1] if detail else str(exc)
            raise ServiceUnavailableError("Camera", f"capture failed: {tail[:300]}") from exc
        if not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise ServiceUnavailableError("Camera", "capture produced an empty image")
        return PhotoResult(path=target)

    # --- microphone ------------------------------------------------------

    def mic_available(self) -> bool:
        return self.runner.can_run("pactl")

    async def record_mic(self, seconds: int) -> RecordingResult:
        """Record the microphone for ``seconds`` seconds to an MP3 file."""
        self._require("Microphone", "ffmpeg")
        seconds = max(1, min(int(seconds), self.settings.mic_max_seconds))
        self._media_dir.mkdir(parents=True, exist_ok=True)
        target = self._media_dir / f"mic-{uuid4().hex}.mp3"
        command = (
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "pulse",
            "-i",
            self.settings.mic_source,
            "-t",
            str(seconds),
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            "-y",
            str(target),
        )
        try:
            await self.runner.run(
                command, timeout=self.settings.timeout_quick_seconds * 2 + seconds
            )
        except CommandFailedError as exc:
            target.unlink(missing_ok=True)
            detail = (exc.stderr or "").strip().splitlines()
            tail = detail[-1] if detail else str(exc)
            raise ServiceUnavailableError("Microphone", f"recording failed: {tail[:300]}") from exc
        if not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise ServiceUnavailableError("Microphone", "recording produced an empty file")
        return RecordingResult(path=target)

    # --- helpers ---------------------------------------------------------

    def _require(self, capability: str, binary: str) -> None:
        if not self.runner.can_run(binary):
            raise ServiceUnavailableError(capability, f"{binary} is not installed")

    async def stop_all(self) -> None:
        """Cancel any in-flight supervisor tasks (used on shutdown)."""
        tasks = list(self._playback_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
