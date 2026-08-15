"""Tests for the permission model."""

from __future__ import annotations

import pytest

from opencode_telegram_controller.core.permissions import (
    Permission,
    PermissionDenied,
    PermissionRegistry,
)


def make_registry() -> PermissionRegistry:
    return PermissionRegistry(admin_user_ids=[123], read_only_user_ids=[456])


async def test_admin_gets_full_permission():
    registry = make_registry()
    assert registry.level_for(123) == Permission.DESTRUCTIVE
    assert registry.can(123, Permission.DESTRUCTIVE)


async def test_read_only_user_only_gets_read():
    registry = make_registry()
    assert registry.level_for(456) == Permission.READ
    assert registry.can(456, Permission.READ)
    assert not registry.can(456, Permission.CONTROL)
    assert not registry.can(456, Permission.DESTRUCTIVE)


async def test_unknown_user_defaults_to_read_only():
    registry = make_registry()
    assert registry.level_for(789) == Permission.READ
    assert registry.can(789, Permission.READ)
    assert not registry.can(789, Permission.DESTRUCTIVE)


async def test_require_accepts_sufficient_level():
    registry = make_registry()
    registry.require(123, Permission.DESTRUCTIVE)
    registry.require(123, Permission.READ)


async def test_require_denies_insufficient_level():
    registry = make_registry()
    with pytest.raises(PermissionDenied):
        registry.require(456, Permission.CONTROL)
    with pytest.raises(PermissionDenied):
        registry.require(456, Permission.DESTRUCTIVE)


async def test_permission_for_command_mapping():
    from opencode_telegram_controller.core.permissions import permission_for_command

    assert permission_for_command("/status") == Permission.READ
    assert permission_for_command("/vpn") == Permission.CONTROL
    assert permission_for_command("/reboot") == Permission.DESTRUCTIVE
    assert permission_for_command("/unknown") == Permission.READ


async def test_media_and_stream_commands_require_control():
    from opencode_telegram_controller.core.permissions import permission_for_command

    for command in (
        "/vpn_change",
        "/cambiar",
        "/playback",
        "/photo",
        "/record_mic",
        "/stream",
        "/stream_stop",
    ):
        assert permission_for_command(command) == Permission.CONTROL, command
