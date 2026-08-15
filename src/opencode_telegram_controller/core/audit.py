"""Audit logging for administrative actions.

Every mutating action performed through the bot is recorded in the SQLite
``audit_log`` table. Only validated parameters are stored: parameter values are
truncated and keys matching common secret names are redacted, so tokens,
passwords or credentials never reach the audit log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..database import Database
from ..models import utcnow

_MAX_PARAM_LENGTH = 200
_REDACTED_KEY_PARTS = ("token", "secret", "password", "passwd", "credential", "api_key")


@dataclass
class AuditEntry:
    id: int | None
    ts: datetime
    user_id: int
    action: str
    target: str | None
    params: str | None
    result: str
    error: str | None


def sanitize_params(params: dict[str, Any] | None) -> str | None:
    """Serialize parameters for the audit log, redacting and truncating values."""
    if not params:
        return None
    cleaned: dict[str, str] = {}
    for key, value in params.items():
        lowered = str(key).lower()
        if any(part in lowered for part in _REDACTED_KEY_PARTS):
            cleaned[str(key)] = "[REDACTED]"
            continue
        text = str(value)
        if len(text) > _MAX_PARAM_LENGTH:
            text = text[: _MAX_PARAM_LENGTH - 3] + "..."
        cleaned[str(key)] = text
    return json.dumps(cleaned, ensure_ascii=True, sort_keys=True)


class AuditLogger:
    """Records administrative actions into the SQLite audit log."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        user_id: int,
        action: str,
        target: str | None = None,
        params: dict[str, Any] | None = None,
        result: str = "success",
        error: str | None = None,
    ) -> None:
        await self._db.conn.execute(
            "INSERT INTO audit_log (ts, user_id, action, target, params, result, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow().isoformat(),
                user_id,
                action,
                target,
                sanitize_params(params),
                result,
                error,
            ),
        )
        await self._db.conn.commit()

    async def list_recent(self, limit: int = 20) -> list[AuditEntry]:
        cur = await self._db.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, limit),)
        )
        rows = await cur.fetchall()
        return [
            AuditEntry(
                id=row["id"],
                ts=datetime.fromisoformat(row["ts"]),
                user_id=row["user_id"],
                action=row["action"],
                target=row["target"],
                params=row["params"],
                result=row["result"],
                error=row["error"],
            )
            for row in rows
        ]
