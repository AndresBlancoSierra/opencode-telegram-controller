"""Shared building blocks for the service layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

from ..config import Settings
from ..core.process import CommandRunner

T = TypeVar("T")


class ServiceUnavailableError(Exception):
    """Raised when the backing tool or daemon for a capability is unavailable.

    The message is user-facing and must not contain stack traces or raw
    command output.
    """

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__(f"{capability} unavailable: {reason}")
        self.capability = capability
        self.reason = reason


@dataclass
class ServiceContext:
    """Dependencies shared by every capability service."""

    settings: Settings
    runner: CommandRunner
