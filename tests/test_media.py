"""Tests for MediaManager: playback supervision, camera photos and mic recording."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencode_telegram_controller.config import Settings
from opencode_telegram_controller.core.process import CommandFailedError, CommandRunner
from opencode_telegram_controller.services import media as media_mod
from opencode_telegram_controller.services.base import ServiceUnavailableError
from opencode_telegram_controller.services.media import MediaManager

MAX_MB = 2


def make_manager(tmp_path, *, can_run=None, **overrides) -> tuple[MediaManager, list]:
    settings = Settings(
        telegram_bot_token="x",
        allowed_user_ids=[1],
        data_dir=tmp_path,
        media_max_download_mb=MAX_MB,
        **overrides,
    )
    runner = CommandRunner()
    manager = MediaManager(settings=settings, runner=runner)
    calls: list = []

    async def fake_run(args, **kwargs):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, ok=True, stdout="", stderr="")

    manager.runner.run = fake_run  # type: ignore[method-assign]
    if can_run is not None:
        manager.runner.can_run = can_run  # type: ignore[method-assign]
    return manager, calls


def make_spawn(manager, proc) -> list:
    spawned: list = []

    async def fake_spawn(args, **kwargs):
        spawned.append(tuple(args))
        return proc

    manager.runner.spawn = fake_spawn  # type: ignore[method-assign]
    return spawned


def fake_proc(*, blocked=False) -> SimpleNamespace:
    proc = SimpleNamespace(pid=999, returncode=None)
    pending: list = []

    async def fake_wait():
        if not blocked:
            return 0
        future = asyncio.get_running_loop().create_future()
        pending.append(future)
        result = await future
        proc.returncode = result
        return result

    proc.wait = fake_wait  # type: ignore[attr-defined]
    proc._pending = pending  # type: ignore[attr-defined]
    return proc


class FakeBot:
    def __init__(self, *, file_size=1000, file_path="files/sample.mp3"):
        self.file_size = file_size
        self.file_path = file_path
        self.downloaded: list[tuple[str, Path]] = []

    async def get_file(self, file_id):
        return SimpleNamespace(file_id=file_id, file_size=self.file_size, file_path=self.file_path)

    async def download_file(self, file_path, destination):
        self.downloaded.append((file_path, destination))
        destination.write_bytes(b"media")
        return destination


async def test_play_audio_uses_background_mpv(tmp_path):
    manager, _ = make_manager(tmp_path, can_run=lambda name: name == "mpv")
    target = tmp_path / "media" / "x.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"audio")
    proc = fake_proc()
    spawned = make_spawn(manager, proc)
    await manager.play_audio(target)
    cmd = spawned[0]
    assert cmd[0] == "mpv"
    assert "--no-video" in cmd
    assert "--audio-display=no" in cmd
    assert "--keep-open=no" in cmd
    assert cmd[-1] == str(target)


async def test_play_video_uses_fullscreen_mpv(tmp_path):
    manager, _ = make_manager(tmp_path, can_run=lambda name: name == "mpv")
    target = tmp_path / "media" / "x.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"video")
    proc = fake_proc()
    spawned = make_spawn(manager, proc)
    await manager.play_video(target)
    cmd = spawned[0]
    assert cmd[0] == "mpv"
    assert "--fullscreen" in cmd
    assert "--fs-screen=current" in cmd
    assert "--ontop" in cmd
    assert "--keep-open=no" in cmd


async def test_play_requires_mpv(tmp_path):
    manager, _ = make_manager(tmp_path, can_run=lambda name: False)
    target = tmp_path / "media" / "x.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"audio")
    with pytest.raises(ServiceUnavailableError, match="mpv"):
        await manager.play_audio(target)


async def test_playback_removes_file_when_it_finishes(tmp_path):
    manager, _ = make_manager(tmp_path, can_run=lambda name: name == "mpv")
    target = tmp_path / "media" / "x.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"audio")
    proc = fake_proc()
    make_spawn(manager, proc)
    await manager.play_audio(target)
    await asyncio.sleep(0.05)
    assert not target.exists()


async def test_playback_kills_mpv_when_exceeding_limit(monkeypatch, tmp_path):
    manager, _ = make_manager(tmp_path, can_run=lambda name: name == "mpv", playback_max_seconds=1)
    target = tmp_path / "media" / "x.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"audio")
    proc = fake_proc(blocked=True)
    make_spawn(manager, proc)
    sent: list = []

    async def fake_signal_group(p, sig):
        sent.append(sig)

    monkeypatch.setattr(media_mod, "signal_group", fake_signal_group)
    await manager.play_audio(target)
    await asyncio.sleep(1.5)
    assert signal.SIGTERM in sent
    proc._pending[-1].set_result(0)
    await asyncio.sleep(0.05)
    assert signal.SIGKILL not in sent
    assert not target.exists()


async def test_download_to_temp_writes_under_media_dir(tmp_path):
    manager, _ = make_manager(tmp_path)
    bot = FakeBot()
    path = await manager.download_to_temp(bot, "file_id_1", suffix=".mp3")
    assert path.parent == tmp_path / "media"
    assert path.name.startswith("play-")
    assert path.name.endswith(".mp3")
    assert path.read_bytes() == b"media"
    assert bot.downloaded == [("files/sample.mp3", path)]


async def test_download_to_temp_rejects_large_file(tmp_path):
    manager, _ = make_manager(tmp_path)
    bot = FakeBot(file_size=MAX_MB * 1024 * 1024 + 1)
    with pytest.raises(ServiceUnavailableError, match="too large"):
        await manager.download_to_temp(bot, "file_id_1", suffix=".mp3")
    assert bot.downloaded == []


async def test_download_to_temp_requires_file_path(tmp_path):
    manager, _ = make_manager(tmp_path)
    bot = FakeBot(file_path=None)
    with pytest.raises(ServiceUnavailableError, match="resolve"):
        await manager.download_to_temp(bot, "file_id_1", suffix=".mp3")


async def test_photo_uses_ffmpeg_v4l2(tmp_path):
    manager, calls = make_manager(
        tmp_path,
        can_run=lambda name: name == "ffmpeg",
        camera_device="/dev/video9",
        camera_resolution="640x480",
    )

    async def fake_run(args, **kwargs):
        calls.append(tuple(args))
        Path(args[-1]).write_bytes(b"\xff\xd8jpeg")
        return SimpleNamespace(returncode=0, ok=True, stdout="", stderr="")

    manager.runner.run = fake_run  # type: ignore[method-assign]
    manager.camera_available = lambda: True  # type: ignore[method-assign]
    result = await manager.photo()
    assert result.path.exists()
    cmd = calls[0]
    assert cmd[0] == "ffmpeg"
    assert ("-f", "v4l2") in zip(cmd, cmd[1:], strict=False)
    assert "-video_size" in cmd
    assert "640x480" in cmd
    assert "/dev/video9" in cmd


async def test_photo_missing_camera_device(tmp_path):
    manager, _ = make_manager(
        tmp_path, can_run=lambda name: name == "ffmpeg", camera_device="/dev/not-a-camera"
    )
    manager.camera_available = lambda: False  # type: ignore[method-assign]
    with pytest.raises(ServiceUnavailableError, match="camera device"):
        await manager.photo()


async def test_photo_empty_output_raises(tmp_path):
    manager, _ = make_manager(tmp_path, can_run=lambda name: name == "ffmpeg")
    manager.camera_available = lambda: True  # type: ignore[method-assign]

    async def fake_run(args, **kwargs):
        return SimpleNamespace(returncode=0, ok=True, stdout="", stderr="")

    manager.runner.run = fake_run  # type: ignore[method-assign]
    with pytest.raises(ServiceUnavailableError, match="empty image"):
        await manager.photo()


async def test_photo_command_failure_maps_to_unavailable(tmp_path):
    manager, _ = make_manager(tmp_path, can_run=lambda name: name == "ffmpeg")
    manager.camera_available = lambda: True  # type: ignore[method-assign]

    async def fake_run(args, **kwargs):
        raise CommandFailedError("fail", returncode=1, stderr="Error: no space left on device\n")

    manager.runner.run = fake_run  # type: ignore[method-assign]
    with pytest.raises(ServiceUnavailableError, match="no space left on device"):
        await manager.photo()


async def test_record_mic_uses_pulse_mp3(tmp_path):
    manager, calls = make_manager(
        tmp_path,
        can_run=lambda name: name == "ffmpeg",
        mic_source="alsa_input.test",
    )

    async def fake_run(args, **kwargs):
        calls.append(tuple(args))
        Path(args[-1]).write_bytes(b"id3")
        return SimpleNamespace(returncode=0, ok=True, stdout="", stderr="")

    manager.runner.run = fake_run  # type: ignore[method-assign]
    result = await manager.record_mic(5)
    assert result.path.exists()
    cmd = calls[0]
    assert cmd[0] == "ffmpeg"
    assert ("-f", "pulse") in zip(cmd, cmd[1:], strict=False)
    assert "alsa_input.test" in cmd
    assert "-t" in cmd and "5" in cmd
    assert "libmp3lame" in cmd


async def test_record_mic_clamps_to_max(tmp_path):
    manager, calls = make_manager(
        tmp_path,
        can_run=lambda name: name == "ffmpeg",
        mic_max_seconds=30,
    )

    async def fake_run(args, **kwargs):
        calls.append(tuple(args))
        Path(args[-1]).write_bytes(b"id3")
        return SimpleNamespace(returncode=0, ok=True, stdout="", stderr="")

    manager.runner.run = fake_run  # type: ignore[method-assign]
    await manager.record_mic(999)
    t_index = calls[0].index("-t")
    assert calls[0][t_index + 1] == "30"


async def test_record_mic_empty_output_raises(tmp_path):
    manager, _ = make_manager(tmp_path, can_run=lambda name: name == "ffmpeg")

    async def fake_run(args, **kwargs):
        return SimpleNamespace(returncode=0, ok=True, stdout="", stderr="")

    manager.runner.run = fake_run  # type: ignore[method-assign]
    with pytest.raises(ServiceUnavailableError, match="empty file"):
        await manager.record_mic(3)


async def test_stop_all_cancels_supervisors(tmp_path):
    manager, _ = make_manager(
        tmp_path, can_run=lambda name: name == "mpv", playback_max_seconds=3600
    )
    target = tmp_path / "media" / "x.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"audio")
    proc = fake_proc(blocked=True)
    make_spawn(manager, proc)
    await manager.play_audio(target)
    await asyncio.sleep(0.05)
    assert manager._playback_tasks
    await manager.stop_all()
    assert not manager._playback_tasks
    assert not target.exists()
