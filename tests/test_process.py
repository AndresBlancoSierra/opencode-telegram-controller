"""Tests for the safe CommandRunner (fixed args, no shell, timeouts)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opencode_telegram_controller.core import process
from opencode_telegram_controller.core.process import (
    CommandError,
    CommandFailedError,
    CommandNotFoundError,
    CommandRunner,
    CommandTimeoutError,
)


def make_fake_subprocess(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    pid: int = 4242,
    communicate=None,
):
    """Duck-typed asyncio subprocess. ``communicate`` defaults to instant."""
    if communicate is None:
        communicate = _instant_communicate(stdout, stderr, returncode)

    async def fake_wait():
        return returncode

    proc = SimpleNamespace(
        pid=pid,
        returncode=returncode,
        communicate=communicate,
        wait=fake_wait,
    )
    return proc


def _instant_communicate(stdout: str, stderr: str, returncode: int):
    async def communicate():
        return stdout.encode(), stderr.encode()

    return communicate


def blocking_communicate():
    """A communicate() that never resolves but is cancellable."""

    async def communicate():
        await asyncio.get_running_loop().create_future()
        return b"", b""

    return communicate


class _Executor:
    """Replaces asyncio.create_subprocess_exec with scripted results."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.results = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            self.calls.append((args, kwargs))
            spec = self.results.pop(0) if self.results else {}
            return make_fake_subprocess(
                stdout=spec.get("stdout", ""),
                stderr=spec.get("stderr", ""),
                returncode=spec.get("returncode", 0),
                communicate=spec.get("communicate"),
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    def queue(self, *, stdout: str = "", stderr: str = "", returncode: int = 0, communicate=None):
        self.results.append(
            {
                "stdout": stdout,
                "stderr": stderr,
                "returncode": returncode,
                "communicate": communicate,
            }
        )


def make_runner(monkeypatch, **kwargs) -> tuple[CommandRunner, _Executor]:
    executor = _Executor(monkeypatch)
    runner = CommandRunner(default_timeout=5.0, **kwargs)
    runner.resolve = lambda name: name  # type: ignore[method-assign]
    return runner, executor


async def test_run_captures_output(monkeypatch):
    runner, executor = make_runner(monkeypatch)
    executor.queue(stdout="one\ntwo\n", returncode=0)
    result = await runner.run(["docker", "ps", "--format", "{{.Names}}"])
    assert result.returncode == 0
    assert result.stdout == "one\ntwo\n"
    assert result.ok
    args, kwargs = executor.calls[0]
    assert tuple(args) == ("docker", "ps", "--format", "{{.Names}}")
    assert kwargs["start_new_session"] is True
    assert "env" in kwargs


async def test_run_uses_extra_env(monkeypatch):
    runner, executor = make_runner(monkeypatch, extra_env={"DOCKER_HOST": "tcp://x"})
    executor.queue()
    await runner.run(["docker", "ps"])
    _, kwargs = executor.calls[0]
    assert kwargs["env"]["DOCKER_HOST"] == "tcp://x"


async def test_run_raises_on_nonzero(monkeypatch):
    runner, executor = make_runner(monkeypatch)
    executor.queue(stderr="boom", returncode=2)
    with pytest.raises(CommandFailedError) as exc_info:
        await runner.run(["fail", "cmd"])
    assert exc_info.value.returncode == 2
    assert "boom" in exc_info.value.stderr


async def test_run_without_check_returns_nonzero(monkeypatch):
    runner, executor = make_runner(monkeypatch)
    executor.queue(stderr="boom", returncode=1)
    result = await runner.run(["fail", "cmd"], check=False)
    assert result.returncode == 1
    assert not result.ok


async def test_run_empty_args_rejected(monkeypatch):
    runner, executor = make_runner(monkeypatch)
    with pytest.raises(CommandError):
        await runner.run([])


async def test_run_unknown_binary(monkeypatch):
    def fake_which(name):
        return None

    monkeypatch.setattr(process.shutil, "which", fake_which)
    runner = CommandRunner()
    with pytest.raises(CommandNotFoundError):
        await runner.run(["definitely-not-a-real-binary-xyz"])


async def test_can_run_detects_binary(monkeypatch):
    def fake_which(name):
        return "/usr/bin/true" if name == "true" else None

    monkeypatch.setattr(process.shutil, "which", fake_which)
    runner = CommandRunner()
    assert runner.can_run("true")
    assert not runner.can_run("nope-xyz")


async def test_run_timeout_kills_group(monkeypatch):
    killed = []
    runner, executor = make_runner(monkeypatch)
    executor.queue(communicate=blocking_communicate())

    async def fake_terminate_group(proc):
        killed.append(proc.pid)

    monkeypatch.setattr(process, "_terminate_group", fake_terminate_group)
    runner._default_timeout = 0.05
    with pytest.raises(CommandTimeoutError):
        await runner.run(["slow", "cmd"])
    assert killed == [4242]
