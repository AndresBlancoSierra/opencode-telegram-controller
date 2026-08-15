"""Tests for the confirmation manager and expiry semantics."""

from __future__ import annotations

import time

import pytest

from opencode_telegram_controller.core.confirmation import (
    ConfirmationManager,
    NoPendingConfirmation,
)


async def test_request_and_confirm():
    manager = ConfirmationManager(timeout_seconds=60)
    manager.request(123, "reboot", {"mode": "hard"})
    pending = manager.pending(123, "reboot")
    assert pending is not None
    assert pending.params == {"mode": "hard"}

    confirmed = manager.confirm(123, "reboot")
    assert confirmed.action == "reboot"
    assert manager.pending(123, "reboot") is None


async def test_confirm_without_request_raises():
    manager = ConfirmationManager(timeout_seconds=60)
    with pytest.raises(NoPendingConfirmation):
        manager.confirm(123, "shutdown")


async def test_expired_confirmation_is_ignored():
    manager = ConfirmationManager(timeout_seconds=1)
    manager.request(123, "sleep")
    time.sleep(1.1)
    assert manager.pending(123, "sleep") is None
    with pytest.raises(NoPendingConfirmation):
        manager.confirm(123, "sleep")


async def test_request_replaces_previous():
    manager = ConfirmationManager(timeout_seconds=60)
    manager.request(123, "reboot", {"mode": "a"})
    manager.request(123, "reboot", {"mode": "b"})
    assert manager.confirm(123, "reboot").params == {"mode": "b"}


async def test_confirmations_are_scoped_per_user():
    manager = ConfirmationManager(timeout_seconds=60)
    manager.request(123, "reboot")
    with pytest.raises(NoPendingConfirmation):
        manager.confirm(456, "reboot")


async def test_dismiss_removes_without_executing():
    manager = ConfirmationManager(timeout_seconds=60)
    manager.request(123, "reboot")
    assert manager.dismiss(123, "reboot") is True
    assert manager.pending(123, "reboot") is None
    assert manager.dismiss(123, "reboot") is False


async def test_pending_for_user_lists_only_valid():
    manager = ConfirmationManager(timeout_seconds=60)
    manager.request(123, "reboot")
    manager.request(456, "lock")
    manager.request(123, "shutdown")
    pending = manager.pending_for_user(123)
    assert {p.action for p in pending} == {"reboot", "shutdown"}


async def test_purge_expired_removes_stale():
    manager = ConfirmationManager(timeout_seconds=1)
    manager.request(123, "reboot")
    time.sleep(1.1)
    manager.request(456, "sleep")
    purged = manager.purge_expired()
    assert purged == 1
    assert manager.pending(456, "sleep") is not None


async def test_positive_timeout_required():
    with pytest.raises(ValueError):
        ConfirmationManager(timeout_seconds=0)
