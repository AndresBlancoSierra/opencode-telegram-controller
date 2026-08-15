"""Cross-cutting building blocks for the PC Control capabilities.

* :mod:`opencode_telegram_controller.core.process` — safe subprocess execution
* :mod:`opencode_telegram_controller.core.permissions` — permission model
* :mod:`opencode_telegram_controller.core.confirmation` — action confirmations
* :mod:`opencode_telegram_controller.core.audit` — audit logging
"""

from __future__ import annotations

from .audit import AuditEntry, AuditLogger
from .confirmation import (
    ConfirmationManager,
    NoPendingConfirmation,
    PendingConfirmation,
)
from .permissions import Permission, PermissionDenied, PermissionRegistry
from .process import (
    CommandError,
    CommandFailedError,
    CommandNotFoundError,
    CommandRunner,
    CommandTimeoutError,
    ProcessResult,
)

__all__ = [
    "AuditEntry",
    "AuditLogger",
    "CommandError",
    "CommandFailedError",
    "CommandNotFoundError",
    "CommandRunner",
    "CommandTimeoutError",
    "ConfirmationManager",
    "NoPendingConfirmation",
    "PendingConfirmation",
    "Permission",
    "PermissionDenied",
    "PermissionRegistry",
    "ProcessResult",
]
