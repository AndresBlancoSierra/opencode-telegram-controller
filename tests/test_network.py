"""Tests for NetworkManager: pure parsers and read-only queries."""

from __future__ import annotations

import json
from types import SimpleNamespace

from opencode_telegram_controller.services.network import (
    NetworkManager,
    _hex_to_ipv4,
    parse_resolv_conf,
    parse_route_table,
)

ROUTE_TEXT = """Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT
eno1\t00000000\t0102A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0
eno1\t00000000\t0102A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0
"""


def make_manager(monkeypatch, *, files=None, script=None, can_run=None):
    from opencode_telegram_controller.config import Settings
    from opencode_telegram_controller.core.process import CommandRunner

    settings = Settings(telegram_bot_token="x", allowed_user_ids=[1])
    manager = NetworkManager(settings=settings, runner=CommandRunner())
    calls = []

    if files is not None:

        async def fake_read(path: str) -> str:
            if path not in files:
                raise AssertionError(f"unexpected read {path}")
            return files[path]

        manager._read = fake_read  # type: ignore[method-assign]

    if script is not None:

        async def fake_run(args, **kwargs):
            calls.append(args)
            if script is None:
                raise AssertionError(f"unscripted run {args!r}")
            step = script.get(args)
            if step is None:
                raise AssertionError(f"unscripted run {args!r}")
            if callable(step):
                return step(args)
            returncode = step.get("returncode", 0)
            return SimpleNamespace(
                returncode=returncode,
                ok=returncode == 0,
                stdout=step.get("stdout", ""),
                stderr=step.get("stderr", ""),
            )

        manager.runner.run = fake_run  # type: ignore[method-assign]

    if can_run is not None:
        manager.runner.can_run = can_run  # type: ignore[method-assign]
    return manager, calls


def test_hex_to_ipv4():
    assert _hex_to_ipv4("0102A8C0") == "192.168.2.1"
    assert _hex_to_ipv4("") == ""
    assert _hex_to_ipv4("not-hex") == ""


def test_parse_route_table_finds_default():
    assert parse_route_table(ROUTE_TEXT) == "192.168.2.1"


def test_parse_route_table_no_default():
    text = "Iface\tDestination\tGateway\neno1\t0102A8C0\t00000000\n"
    assert parse_route_table(text) is None


def test_parse_resolv_conf():
    servers, domains = parse_resolv_conf(
        "nameserver 1.1.1.1\nnameserver 9.9.9.9\nsearch lan home\n"
    )
    assert servers == ["1.1.1.1", "9.9.9.9"]
    assert domains == ["lan", "home"]


async def test_gateway_configured_from_proc(monkeypatch):
    manager, calls = make_manager(monkeypatch, files={"/proc/net/route": ROUTE_TEXT})
    assert await manager.gateway_configured() is True
    assert await manager.gateway() == "192.168.2.1"


async def test_network_info_from_ip_json(monkeypatch):
    payload = [
        {"ifname": "lo", "operstate": "unknown", "addr_info": []},
        {
            "ifname": "wlan0",
            "operstate": "up",
            "addr_info": [{"family": "inet", "scope": "global", "local": "192.168.1.20"}],
        },
        {"ifname": "eno1", "operstate": "down", "addr_info": []},
    ]
    manager, calls = make_manager(
        monkeypatch,
        script={("ip", "-j", "addr", "show"): {"stdout": json.dumps(payload)}},
        can_run=lambda name: name == "ip",
    )
    info = await manager.network_info()
    assert [i.name for i in info.interfaces] == ["wlan0", "eno1"]
    assert info.active_interfaces[0].ipv4 == "192.168.1.20"
    assert ("ip", "-j", "addr", "show") in calls


async def test_dns_detects_systemd_resolved(monkeypatch):
    manager, calls = make_manager(
        monkeypatch,
        files={"/etc/resolv.conf": "nameserver 127.0.0.53\nsearch lan\n"},
        can_run=lambda name: name == "resolvectl",
    )
    snapshot = await manager.dns()
    assert snapshot.backend == "systemd-resolved"
    assert snapshot.servers == ["127.0.0.53"]
    assert snapshot.search_domains == ["lan"]


async def test_dns_plain_backend_without_tools(monkeypatch):
    manager, calls = make_manager(
        monkeypatch,
        files={"/etc/resolv.conf": "nameserver 1.1.1.1\n"},
        can_run=lambda name: False,
    )
    snapshot = await manager.dns()
    assert snapshot.backend == "plain"
    assert snapshot.servers == ["1.1.1.1"]


async def test_public_ip_uses_fixed_providers(monkeypatch):

    import opencode_telegram_controller.services.network as network_mod

    manager, calls = make_manager(monkeypatch)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if url == "https://api.ipify.org":
                return SimpleNamespace(status_code=500, text="")
            return SimpleNamespace(status_code=200, text=" 8.8.8.8 ")

    monkeypatch.setattr(network_mod.httpx, "AsyncClient", FakeClient)
    assert await manager.public_ip() == "8.8.8.8"


async def test_public_ip_returns_none_when_all_fail(monkeypatch):
    import httpx

    import opencode_telegram_controller.services.network as network_mod

    manager, calls = make_manager(monkeypatch)

    class DownClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            raise httpx.ConnectError("down")

    monkeypatch.setattr(network_mod.httpx, "AsyncClient", DownClient)
    assert await manager.public_ip() is None
