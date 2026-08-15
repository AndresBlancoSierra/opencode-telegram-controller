"""Tests for LiveStreamManager: wf-recorder clips sent to the chat."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencode_telegram_controller.config import Settings
from opencode_telegram_controller.core.process import CommandRunner
from opencode_telegram_controller.services.stream import LiveStreamManager, StreamError


class FakeStreamBot:
    def __init__(self):
        self.videos: list[tuple[int, str, str | None]] = []
        self.messages: list[tuple[int, str]] = []

    async def send_video(self, chat_id, path, caption=None):
        self.videos.append((chat_id, str(path), caption))

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def make_stream(
    tmp_path,
    *,
    clip_seconds=0.05,
    returncode=0,
    with_audio=True,
    can_run=None,
) -> tuple[LiveStreamManager, list, FakeStreamBot]:
    settings = Settings(
        telegram_bot_token="x",
        allowed_user_ids=[1],
        data_dir=tmp_path,
        stream_clip_seconds=clip_seconds,
        stream_framerate=15,
        stream_with_audio=with_audio,
    )
    runner = CommandRunner()
    bot = FakeStreamBot()
    manager = LiveStreamManager(settings=settings, runner=runner, bot=bot)
    spawned: list = []

    async def fake_wait():
        return returncode

    async def fake_spawn(args, **kwargs):
        spawned.append(tuple(args))
        Path(args[-1]).write_bytes(b"mkv")
        return SimpleNamespace(pid=7, returncode=returncode, wait=fake_wait)

    manager.runner.spawn = fake_spawn  # type: ignore[method-assign]
    if can_run is not None:
        manager.runner.can_run = can_run  # type: ignore[method-assign]
    return manager, spawned, bot


async def wait_for_condition(condition, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_start_requires_wf_recorder(tmp_path):
    manager, _, _ = make_stream(tmp_path, can_run=lambda name: False)
    with pytest.raises(StreamError, match="wf-recorder"):
        await manager.start(chat_id=1)
    assert not manager.is_streaming(1)


async def test_start_sends_clips_with_audio(tmp_path):
    manager, spawned, bot = make_stream(tmp_path)
    await manager.start(chat_id=1)
    assert manager.is_streaming(1)
    await wait_for_condition(lambda: len(bot.videos) >= 2)
    cmd = spawned[0]
    assert cmd[0] == "wf-recorder"
    assert "-r" in cmd and "15" in cmd
    assert "-c" in cmd and "libx264" in cmd
    assert "-a" in cmd
    assert "-f" in cmd and cmd[-1].startswith(str(tmp_path / "streams"))
    assert bot.videos[0][0] == 1
    assert await manager.stop(1) is True
    await wait_for_condition(lambda: not manager.is_streaming(1))


async def test_start_rejects_second_stream(tmp_path):
    manager, _, _ = make_stream(tmp_path)
    await manager.start(chat_id=1)
    try:
        with pytest.raises(StreamError, match="already running"):
            await manager.start(chat_id=1)
    finally:
        await manager.stop(1)


async def test_stop_without_stream_returns_false(tmp_path):
    manager, _, _ = make_stream(tmp_path)
    assert await manager.stop(1) is False


async def test_clip_failure_stops_and_notifies(tmp_path):
    manager, spawned, bot = make_stream(tmp_path, returncode=1)
    await manager.start(chat_id=1)
    await wait_for_condition(lambda: not manager.is_streaming(1))
    assert "-a" in spawned[0]
    assert "-a" not in spawned[1]
    assert bot.messages and "Stream stopped" in bot.messages[0][1]


async def test_stop_all_cleans_streams(tmp_path):
    manager, _, _ = make_stream(tmp_path)
    await manager.start(chat_id=1)
    await wait_for_condition(lambda: manager.is_streaming(1))
    await manager.stop_all()
    await wait_for_condition(lambda: not manager.is_streaming(1))
