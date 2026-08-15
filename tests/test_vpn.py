"""Tests for the VPN capability: parsers, NordVpnProvider behavior and security."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencode_telegram_controller.services.base import ServiceUnavailableError
from opencode_telegram_controller.services.vpn import (
    NordVpnProvider,
    VpnError,
    VpnTarget,
    build_vpn_manager,
    parse_nordvpn_settings,
    parse_nordvpn_status,
)


def make_provider(monkeypatch, *, script=None):
    from opencode_telegram_controller.config import Settings
    from opencode_telegram_controller.core.process import CommandRunner

    settings = Settings(
        telegram_bot_token="x",
        allowed_user_ids=[1],
        vpn_countries=["us", "germany"],
        vpn_dedicated_server="us4nord.example",
    )
    provider = NordVpnProvider(settings=settings, runner=CommandRunner())

    calls = []

    async def fake_run(args, **kwargs):
        calls.append(args)
        if script is None:
            raise AssertionError(f"unexpected run {args!r}")
        step = script.get(args[:2])
        if step is None:
            raise AssertionError(f"unscripted run {args!r}")
        if callable(step):
            return step(args, kwargs)
        returncode = step.get("returncode", 0)
        return SimpleNamespace(
            returncode=returncode,
            ok=returncode == 0,
            stdout=step.get("stdout", ""),
            stderr=step.get("stderr", ""),
        )

    provider.runner.run = fake_run  # type: ignore[method-assign]
    return provider, calls


def test_parse_nordvpn_status_connected():
    text = (
        "Status: Connected\n"
        "Hostname: us1234.nordvpn.com\n"
        "IP: 10.5.0.2\n"
        "Country: United States\n"
        "Current technology: NORDLYNX\n"
        "Current server: us1234\n"
    )
    status = parse_nordvpn_status(text)
    assert status.connected is True
    assert status.country == "United States"
    assert status.server == "us1234"
    assert status.ip == "10.5.0.2"
    assert status.error is None


def test_parse_nordvpn_status_disconnected():
    status = parse_nordvpn_status("Status: Disconnected\n")
    assert status.connected is False
    assert status.provider == "NordVPN"
    assert status.server is None


def test_parse_nordvpn_status_empty_output():
    status = parse_nordvpn_status("")
    assert status.connected is False
    assert status.provider is None
    assert status.error is not None


def test_parse_nordvpn_settings():
    parsed = parse_nordvpn_settings("Kill Switch: enabled\nNotify: true\n")
    assert parsed == {"Kill Switch": "enabled", "Notify": "true"}


async def test_status_queries_status_and_settings(monkeypatch):
    provider, calls = make_provider(
        monkeypatch,
        script={
            ("nordvpn", "status"): {"stdout": "Status: Connected\nCountry: United States\n"},
            ("nordvpn", "settings"): {"stdout": "Kill Switch: enabled\n"},
        },
    )
    status = await provider.status()
    assert status.connected is True
    assert status.kill_switch is True
    assert calls == [("nordvpn", "status"), ("nordvpn", "settings")]


async def test_connect_valid_country(monkeypatch):
    provider, calls = make_provider(
        monkeypatch,
        script={
            ("nordvpn", "connect"): {"stdout": "Connecting to us.\n"},
            ("nordvpn", "status"): {"stdout": "Status: Connected\nCountry: United States\n"},
            ("nordvpn", "settings"): {"stdout": "Kill Switch: enabled\n"},
        },
    )
    status = await provider.connect(VpnTarget(country="us"))
    assert status.connected is True
    assert calls == [
        ("nordvpn", "connect", "us"),
        ("nordvpn", "status"),
        ("nordvpn", "settings"),
    ]


async def test_connect_failure_raises_vpn_error(monkeypatch):
    provider, calls = make_provider(
        monkeypatch,
        script={
            ("nordvpn", "connect"): {
                "returncode": 1,
                "stderr": "Error: Failed to connect.\nDue to: no daemon.\n",
            },
        },
    )
    with pytest.raises(VpnError, match="no daemon"):
        await provider.connect(VpnTarget(country="us"))


async def test_connect_dedicated_valid_server(monkeypatch):
    provider, calls = make_provider(
        monkeypatch,
        script={
            ("nordvpn", "connect"): {"stdout": "ok\n"},
            ("nordvpn", "status"): {"stdout": "Status: Connected\n"},
            ("nordvpn", "settings"): {"stdout": "Kill Switch: enabled\n"},
        },
    )
    status = await provider.connect_dedicated("us4nord.example")
    assert status.connected is True
    assert calls[0] == ("nordvpn", "connect", "us4nord.example")


async def test_connect_dedicated_rejects_override_server(monkeypatch):
    provider, calls = make_provider(monkeypatch, script={})
    with pytest.raises(VpnError, match="Invalid dedicated VPN server name"):
        await provider.connect_dedicated("--config /evil")
    assert calls == []


async def test_disconnect(monkeypatch):
    provider, calls = make_provider(
        monkeypatch,
        script={
            ("nordvpn", "disconnect"): {"stdout": "Disconnected.\n"},
            ("nordvpn", "status"): {"stdout": "Status: Disconnected\n"},
            ("nordvpn", "settings"): {"stdout": "Kill Switch: disabled\n"},
        },
    )
    status = await provider.disconnect()
    assert status.connected is False
    assert calls == [("nordvpn", "disconnect"), ("nordvpn", "status"), ("nordvpn", "settings")]


async def test_reconnect_disconnects_then_connects(monkeypatch):
    provider, calls = make_provider(
        monkeypatch,
        script={
            ("nordvpn", "disconnect"): {"stdout": "Disconnected.\n"},
            ("nordvpn", "connect"): {"stdout": "Connecting.\n"},
            ("nordvpn", "status"): {"stdout": "Status: Connected\nCountry: United States\n"},
            ("nordvpn", "settings"): {"stdout": "Kill Switch: enabled\n"},
        },
    )
    status = await provider.reconnect()
    assert status.connected is True
    assert calls[0] == ("nordvpn", "disconnect")
    assert calls[1] == ("nordvpn", "connect")
    assert calls[2] == ("nordvpn", "status")


async def test_reconnect_connect_failure_raises_vpn_error(monkeypatch):
    provider, calls = make_provider(
        monkeypatch,
        script={
            ("nordvpn", "disconnect"): {"stdout": "Disconnected.\n"},
            ("nordvpn", "connect"): {
                "returncode": 1,
                "stderr": "Error: Failed to connect.\nDue to: no daemon.\n",
            },
        },
    )
    with pytest.raises(VpnError, match="no daemon"):
        await provider.reconnect()
    assert calls[0] == ("nordvpn", "disconnect")
    assert calls[1] == ("nordvpn", "connect")


async def test_list_countries_parses(monkeypatch):
    provider, calls = make_provider(
        monkeypatch,
        script={("nordvpn", "countries"): {"stdout": "United States\nGermany\nunited states\n"}},
    )
    countries = await provider.list_countries()
    assert "united states" in countries
    assert countries == sorted(set(countries))


def test_build_vpn_manager_unavailable_when_no_cli(monkeypatch):
    from opencode_telegram_controller.config import Settings

    settings = Settings(telegram_bot_token="x", allowed_user_ids=[1])
    from opencode_telegram_controller.core.process import CommandRunner

    runner = CommandRunner()
    runner.can_run = lambda name: False  # type: ignore[method-assign]
    with pytest.raises(ServiceUnavailableError, match="nordvpn is not installed"):
        build_vpn_manager(settings=settings, runner=runner)


def test_resolve_target_rejects_arbitrary_text():
    from opencode_telegram_controller.config import Settings
    from opencode_telegram_controller.core.process import CommandRunner

    settings = Settings(
        telegram_bot_token="x",
        allowed_user_ids=[1],
        vpn_countries=["us", "germany"],
    )
    provider = NordVpnProvider(settings=settings, runner=CommandRunner())
    assert provider.resolve_target(" GERMANY ").country == "germany"
    with pytest.raises(ValueError, match="rm -rf"):
        provider.resolve_target("rm -rf /")
    with pytest.raises(ValueError, match="brazil"):
        provider.resolve_target("brazil")
