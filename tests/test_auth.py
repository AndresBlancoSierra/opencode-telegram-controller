"""Tests for Telegram authorization."""

from __future__ import annotations

from opencode_telegram_controller.auth import AuthorizationService

REAL_INTERVAL = 900.0


def monotonic_clock(start=0.0):
    """Factory for a controllable monotonic clock."""
    state = {"now": start}

    def clock():
        return state["now"]

    def advance(seconds: float):
        state["now"] += seconds

    return clock, advance


async def test_authorized_user_allowed():
    auth = AuthorizationService([123, 456])
    assert auth.is_authorized(123)
    assert auth.is_authorized(456)
    assert not auth.is_authorized(999)


async def test_unauthorized_user_has_no_access():
    auth = AuthorizationService([123])
    assert not auth.is_authorized(999)
    assert not auth.is_authorized(0)
    assert not auth.is_authorized(-1)


async def test_security_event_notified_once_per_user(monkeypatch):
    events = []
    auth = AuthorizationService([123], on_security_event=lambda t: events.append(t))
    clock, advance = monotonic_clock()
    monkeypatch.setattr("opencode_telegram_controller.auth.time.monotonic", clock)
    await auth.handle_unauthorized(999, "mallory")
    await auth.handle_unauthorized(999, "mallory")
    assert len(events) == 1
    assert "999" in events[0]


async def test_security_event_repeats_after_interval(monkeypatch):
    events = []
    auth = AuthorizationService([123], on_security_event=lambda t: events.append(t))
    clock, advance = monotonic_clock()
    monkeypatch.setattr("opencode_telegram_controller.auth.time.monotonic", clock)
    await auth.handle_unauthorized(999, "mallory")
    await auth.handle_unauthorized(999, "mallory")
    advance(REAL_INTERVAL)
    await auth.handle_unauthorized(999, "mallory")
    assert len(events) == 2


async def test_security_event_uses_different_user_bucket(monkeypatch):
    events = []
    auth = AuthorizationService([123], on_security_event=lambda t: events.append(t))
    clock, advance = monotonic_clock()
    monkeypatch.setattr("opencode_telegram_controller.auth.time.monotonic", clock)
    await auth.handle_unauthorized(999, "mallory")
    await auth.handle_unauthorized(1000, "eve")
    assert len(events) == 2
