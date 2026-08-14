"""Abstraction layer over the OpenCode programmatic interface.

The Telegram layer and task machinery depend only on the :class:`OpenCodeAdapter`
protocol defined here. The concrete implementation (currently the CLI adapter)
can be swapped later without rewriting the rest of the system.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class OpenCodeEvent:
    """A single parsed event from the OpenCode JSON event stream."""

    type: str
    session_id: str | None = None
    part_type: str | None = None
    text: str | None = None
    raw: dict = field(default_factory=dict)


class RunHandle(abc.ABC):
    """Handle to a running OpenCode subprocess."""

    @abc.abstractmethod
    async def events(self):  # async iterator of OpenCodeEvent
        raise NotImplementedError

    @abc.abstractmethod
    async def stderr_lines(self) -> list[str]:
        raise NotImplementedError

    @abc.abstractmethod
    async def wait(self) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    async def cancel(self) -> None:
        """Send SIGTERM to the process group and kill it after a grace period."""
        raise NotImplementedError

    @abc.abstractmethod
    async def kill(self) -> None:
        raise NotImplementedError


class OpenCodeAdapter(abc.ABC):
    """Interface for launching and inspecting OpenCode tasks."""

    @abc.abstractmethod
    async def run(self, *, prompt: str, cwd: str, session_id: str | None = None) -> RunHandle:
        raise NotImplementedError

    @abc.abstractmethod
    async def export(self, session_id: str) -> dict:
        """Return the full structured JSON export of an OpenCode session."""
        raise NotImplementedError

    async def session_exists(self, session_id: str) -> bool:
        """Return whether a real OpenCode session with this id exists.

        The default is conservative: assume the session still exists so that
        the flow is never blocked by a false negative from an old CLI.
        """
        return True
