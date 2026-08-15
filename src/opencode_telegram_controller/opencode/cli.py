"""CLI-based OpenCode adapter.

Runs ``opencode run --format json --dir <cwd>`` as an async subprocess per task.
This is the primary integration and was verified against OpenCode 1.15.13:

* ``opencode run <message> --format json`` emits newline-delimited JSON events
  on stdout (``step_start``, ``text``, ``tool_use``, ``step_finish``, ...).
* ``opencode export <sessionID>`` returns the full session as structured JSON,
  used for summaries.
* Sessions can be continued with ``-s <sessionID>``.

The prompt is always passed as a single argument (never through a shell), so
shell metacharacters in user prompts cannot escape into the command line.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from contextlib import suppress
from dataclasses import dataclass, field

from loguru import logger

from .base import OpenCodeAdapter, RunHandle
from .events import parse_line

# Environment variables set by opencode for its own managed servers. When the
# controller is itself running inside an opencode session (development), these
# must be removed so the spawned process starts its own server.
_INHERITED_OPENCODE_VARS = ("OPENCODE", "OPENCODE_PID", "OPENCODE_RUN_ID", "OPENCODE_PROCESS_ROLE")

_CANCEL_GRACE_SECONDS = 5.0

# ``opencode export`` reads the session storage file, which a detached child
# process keeps writing for a moment after the run exits. Right after a run the
# JSON can be transiently truncated; retry briefly instead of giving up.
_EXPORT_RETRY_ATTEMPTS = 4
_EXPORT_RETRY_DELAY = 0.6


@dataclass
class CLIHandle(RunHandle):
    """RunHandle backed by an ``opencode run`` subprocess."""

    process: asyncio.subprocess.Process
    _stderr_buffer: list[str] = field(default_factory=list)
    _stderr_done: asyncio.Event = field(default_factory=asyncio.Event)
    _stderr_task: asyncio.Task | None = None

    async def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        async for raw in self.process.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self._stderr_buffer.append(line)
                if len(self._stderr_buffer) > 200:
                    self._stderr_buffer.pop(0)
        self._stderr_done.set()

    async def start_stderr_drain(self) -> None:
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def events(self):
        assert self.process.stdout is not None
        async for raw in self.process.stdout:
            event = parse_line(raw.decode(errors="replace"))
            if event is not None:
                yield event

    async def stderr_lines(self) -> list[str]:
        if self._stderr_task is not None:
            await asyncio.wait_for(self._stderr_done.wait(), timeout=10)
        return list(self._stderr_buffer)

    async def wait(self) -> int:
        return await self.process.wait()

    def _signal_group(self, sig: signal.Signals) -> None:
        if self.process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(os.getpgid(self.process.pid), sig)

    async def cancel(self) -> None:
        logger.info("Cancelling OpenCode process group pid={}", self.process.pid)
        self._signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=_CANCEL_GRACE_SECONDS)
        except TimeoutError:
            self._signal_group(signal.SIGKILL)
            await self.process.wait()

    async def kill(self) -> None:
        self._signal_group(signal.SIGKILL)
        with suppress(ProcessLookupError):  # pragma: no cover
            await self.process.wait()


class CLIOpenCodeAdapter(OpenCodeAdapter):
    """OpenCode adapter that shells out to the ``opencode`` binary."""

    def __init__(
        self,
        *,
        binary: str = "opencode",
        model: str | None = None,
        agent: str | None = "build",
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self._binary = binary
        self._model = model
        self._agent = agent
        self._extra_args = list(extra_args or [])
        self._base_env = env if env is not None else _clean_env()

    async def run(self, *, prompt: str, cwd: str, session_id: str | None = None) -> RunHandle:
        command = [self._binary, "run", prompt, "--format", "json", "--dir", cwd]
        if session_id:
            command += ["-s", session_id]
        if self._model:
            command += ["-m", self._model]
        if self._agent:
            command += ["--agent", self._agent]
        command += ["--title", "Remote task via Telegram"]
        command += self._extra_args
        logger.info("Starting OpenCode subprocess: {}", " ".join(command[:3]) + " ...")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=self._base_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        handle = CLIHandle(process=process)
        await handle.start_stderr_drain()
        return handle

    async def export(self, session_id: str) -> dict:
        for attempt in range(_EXPORT_RETRY_ATTEMPTS):
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "export",
                session_id,
                env=self._base_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                try:
                    data = json.loads(stdout.decode(errors="replace"))
                except json.JSONDecodeError as exc:
                    if attempt + 1 < _EXPORT_RETRY_ATTEMPTS:
                        logger.warning(
                            "opencode export truncated (attempt {}/{}): {}",
                            attempt + 1,
                            _EXPORT_RETRY_ATTEMPTS,
                            exc,
                        )
                        await asyncio.sleep(_EXPORT_RETRY_DELAY)
                        continue
                    logger.exception("Failed to parse opencode export JSON")
                    return {}
                if isinstance(data, dict):
                    return data
            if attempt + 1 < _EXPORT_RETRY_ATTEMPTS:
                logger.warning(
                    "opencode export failed with code {} (attempt {}/{})",
                    proc.returncode,
                    attempt + 1,
                    _EXPORT_RETRY_ATTEMPTS,
                )
                await asyncio.sleep(_EXPORT_RETRY_DELAY)
                continue
            logger.warning("opencode export failed with code {}", proc.returncode)
            return {}
        return {}

    async def session_exists(self, session_id: str) -> bool:
        """Check whether a real OpenCode session exists.

        Uses ``opencode export`` because it is synchronous (unlike
        ``opencode session list``, whose JSON is written by a detached child
        process after the parent exits, so it is not capturable via pipes).
        A session that fails to export with "session not found" does not exist.
        """
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            "export",
            session_id,
            env=self._base_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True
        output = (stdout + stderr).decode(errors="replace").lower()
        if "session not found" in output:
            return False
        logger.warning("opencode export check failed with code {}", proc.returncode)
        return True


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _INHERITED_OPENCODE_VARS:
        env.pop(key, None)
    return env
