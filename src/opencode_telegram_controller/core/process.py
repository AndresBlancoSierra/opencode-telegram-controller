"""Safe subprocess execution for the PC Control capabilities.

Every manager in :mod:`opencode_telegram_controller.services` executes external
commands through :class:`CommandRunner`. Commands are always built as a fixed
``argv`` list and executed **without a shell**, so text coming from Telegram
can never be interpreted by a shell.

A timeout is mandatory so no external process can block the bot indefinitely.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass

_DECODE_ERRORS = "replace"
_KILL_GRACE_SECONDS = 1.0


class CommandError(Exception):
    """Base class for subprocess execution failures."""


class CommandNotFoundError(CommandError):
    """The requested binary is not available on this machine."""


class CommandTimeoutError(CommandError):
    """The process did not finish within the allowed timeout."""


class CommandFailedError(CommandError):
    """The process exited with a non-zero exit code."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class ProcessResult:
    """Standardized result of a subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner:
    """Runs allowlisted, fixed-argument commands with a mandatory timeout.

    The runner never uses a shell: ``args`` are passed straight to
    :func:`asyncio.create_subprocess_exec`. Argument lists must be built
    internally by the caller from validated data.
    """

    def __init__(
        self,
        *,
        default_timeout: float = 10.0,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._default_timeout = default_timeout
        self._extra_env = dict(extra_env or {})

    def can_run(self, name: str) -> bool:
        """Return whether ``name`` resolves to an executable on PATH."""
        return shutil.which(name) is not None

    def resolve(self, name: str) -> str:
        """Resolve ``name`` to an absolute path, raising if unavailable."""
        path = shutil.which(name)
        if path is None:
            raise CommandNotFoundError(f"Command not found: {name}")
        return path

    async def run(
        self,
        args: Iterable[str],
        *,
        timeout: float | None = None,
        check: bool = True,
        cwd: str | None = None,
    ) -> ProcessResult:
        """Execute ``args`` and return the captured output.

        If ``timeout`` is None the runner's ``default_timeout`` applies.
        When ``check`` is True a non-zero exit code raises
        :class:`CommandFailedError`.
        """
        argv = tuple(str(arg) for arg in args)
        if not argv:
            raise CommandError("Cannot run an empty command")
        binary = argv[0]
        self.resolve(binary)

        env = dict(os.environ) if self._extra_env else os.environ.copy()
        if self._extra_env:
            env.update(self._extra_env)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        limit = timeout if timeout is not None else self._default_timeout
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=limit)
        except TimeoutError:
            await _terminate_group(proc)
            raise CommandTimeoutError(
                f"Command timed out after {limit:g}s: {' '.join(argv)}"
            ) from None

        stdout = stdout_bytes.decode(errors=_DECODE_ERRORS) if stdout_bytes else ""
        stderr = stderr_bytes.decode(errors=_DECODE_ERRORS) if stderr_bytes else ""
        result = ProcessResult(returncode=proc.returncode, stdout=stdout, stderr=stderr)
        if check and not result.ok:
            raise CommandFailedError(
                f"Command failed with exit code {result.returncode}: {' '.join(argv)}",
                returncode=result.returncode,
                stderr=result.stderr[-2000:],
            )
        return result

    async def spawn(
        self,
        args: Iterable[str],
        *,
        cwd: str | None = None,
    ) -> asyncio.subprocess.Process:
        """Start ``args`` detached (own process group, no pipes) without waiting.

        Used for fire-and-forget processes (media playback, screen recording).
        The caller owns the returned :class:`asyncio.subprocess.Process` and is
        responsible for termination and cleanup.
        """
        argv = tuple(str(arg) for arg in args)
        if not argv:
            raise CommandError("Cannot run an empty command")
        self.resolve(argv[0])

        env = dict(os.environ) if self._extra_env else os.environ.copy()
        if self._extra_env:
            env.update(self._extra_env)

        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )


async def signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    """Send ``sig`` to the whole process group of ``proc`` (best effort)."""
    with suppress(ProcessLookupError, PermissionError, OSError):
        pgid = os.getpgid(proc.pid)
        if pgid <= 0 or pgid == os.getpgrp():
            return
        os.killpg(pgid, sig)


async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate the process group (SIGTERM, then SIGKILL)."""
    await signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
        return
    except TimeoutError:
        pass
    await signal_group(proc, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
    except TimeoutError:
        del proc  # pragma: no cover - best effort only
