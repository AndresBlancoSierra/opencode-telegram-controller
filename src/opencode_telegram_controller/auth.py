"""Authorization: Telegram user allowlist.

The bot is designed for a single trusted user. Every incoming update is checked
against ``OTC_ALLOWED_USER_IDS``. Unauthorized users are logged, optionally
reported to the authorized users (rate-limited), and otherwise dropped.
"""

from __future__ import annotations

import time

from loguru import logger

_AUTH_NOTIFY_INTERVAL_SECONDS = 900.0


class AuthorizationService:
    def __init__(self, allowed_user_ids: list[int], on_security_event=None):
        self._allowed = set(allowed_user_ids)
        self._last_notify: dict[int, float] = {}
        self._on_security_event = on_security_event

    def is_authorized(self, user_id: int) -> bool:
        return user_id in self._allowed

    async def handle_unauthorized(self, user_id: int, username: str | None) -> None:
        logger.warning(
            "SECURITY: unauthorized Telegram user rejected: id={} name={}",
            user_id,
            username,
        )
        now = time.monotonic()
        last = self._last_notify.get(user_id, now - _AUTH_NOTIFY_INTERVAL_SECONDS)
        if now - last >= _AUTH_NOTIFY_INTERVAL_SECONDS and self._on_security_event is not None:
            self._last_notify[user_id] = now
            try:
                await self._on_security_event(
                    f"🚫 Unauthorized Telegram access attempt\n"
                    f"User ID: {user_id}\nUsername: {username or 'unknown'}"
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to send security event notification")
