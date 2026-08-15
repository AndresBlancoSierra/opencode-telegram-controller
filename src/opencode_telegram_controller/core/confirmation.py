"""Generic action confirmations for destructive operations.

Destructive commands (reboot, shutdown, sleep, ...) never execute on the first
message. They register a :class:`PendingConfirmation` keyed by ``(user_id,
action)`` with a timestamp and an expiry. The user then replies with a
``/confirm_<action>`` command before the operation runs.

Confirmations are stored in memory (no secrets involved) and expire after a
configurable timeout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class NoPendingConfirmation(Exception):
    """Raised when confirming an action that has no pending confirmation."""


@dataclass
class PendingConfirmation:
    """A not-yet-confirmed destructive action requested by a user."""

    user_id: int
    action: str
    created_at: float
    expires_at: float
    params: dict = field(default_factory=dict)

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at


def action_key(user_id: int, action: str) -> tuple[int, str]:
    return (user_id, action)


class ConfirmationManager:
    """Tracks and expires pending confirmations per user and action."""

    def __init__(self, timeout_seconds: float = 60.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._pending: dict[tuple[int, str], PendingConfirmation] = {}

    def request(
        self,
        user_id: int,
        action: str,
        params: dict | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> PendingConfirmation:
        """Register a confirmation request, replacing any previous one."""
        timeout = timeout_seconds if timeout_seconds is not None else self._timeout_seconds
        now = time.monotonic()
        confirmation = PendingConfirmation(
            user_id=user_id,
            action=action,
            created_at=now,
            expires_at=now + timeout,
            params=dict(params or {}),
        )
        self._pending[action_key(user_id, action)] = confirmation
        return confirmation

    def pending(self, user_id: int, action: str) -> PendingConfirmation | None:
        entry = self._pending.get(action_key(user_id, action))
        if entry is None:
            return None
        if entry.is_expired():
            self._pending.pop(action_key(user_id, action), None)
            return None
        return entry

    def confirm(self, user_id: int, action: str) -> PendingConfirmation:
        """Retrieve and remove a still-valid confirmation.

        Raises :class:`NoPendingConfirmation` when missing or expired.
        """
        entry = self.pending(user_id, action)
        if entry is None:
            raise NoPendingConfirmation(f"No pending confirmation for {action!r} (or it expired)")
        self._pending.pop(action_key(user_id, action), None)
        return entry

    def dismiss(self, user_id: int, action: str) -> bool:
        """Remove a pending confirmation without executing it."""
        return self._pending.pop(action_key(user_id, action), None) is not None

    def pending_for_user(self, user_id: int) -> list[PendingConfirmation]:
        result: list[PendingConfirmation] = []
        for owner, action in list(self._pending):
            if owner == user_id:
                entry = self.pending(user_id, action)
                if entry is not None:
                    result.append(entry)
        return result

    def purge_expired(self) -> int:
        """Remove all expired confirmations; returns how many were purged."""
        now = time.monotonic()
        purged = 0
        for key, entry in list(self._pending.items()):
            if entry.is_expired(now):
                self._pending.pop(key, None)
                purged += 1
        return purged
