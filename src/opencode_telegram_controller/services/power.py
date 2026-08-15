"""Power capability: reboot, shutdown and sleep.

These actions are DESTRUCTIVE: they always require a previously-created
confirmation (:class:`PendingConfirmation`). The manager refuses to execute
without a valid confirmation record that matches the requested action.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..core.confirmation import PendingConfirmation
from ..core.process import CommandRunner

POWER_ACTIONS = frozenset({"reboot", "shutdown", "sleep"})


@dataclass
class PowerActionResult:
    action: str
    executed: bool
    detail: str


class PowerManager:
    """Executes destructive power actions guarded by confirmations."""

    def __init__(self, *, settings: Settings, runner: CommandRunner) -> None:
        self.settings = settings
        self.runner = runner

    def available(self, action: str) -> bool:
        return self.runner.can_run("loginctl")

    def command_for(self, action: str) -> tuple[str, ...]:
        """Return the fixed argv for a power action (never user-controlled)."""
        configured = {
            "reboot": self.settings.power_reboot_command,
            "shutdown": self.settings.power_shutdown_command,
            "sleep": self.settings.power_sleep_command,
        }.get(action)
        if configured:
            return tuple(configured)
        command = {"reboot": "reboot", "shutdown": "poweroff", "sleep": "suspend"}[action]
        return ("loginctl", command)

    async def perform(self, confirmation: PendingConfirmation) -> PowerActionResult:
        action = confirmation.action
        if action not in POWER_ACTIONS:
            raise ValueError(f"Unknown power action: {action!r}")
        command = self.command_for(action)
        await self.runner.run(command, timeout=self.settings.timeout_quick_seconds)
        return PowerActionResult(action=action, executed=True, detail=" ".join(command))
