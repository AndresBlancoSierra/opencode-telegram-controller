"""Tests for the HealthMonitor check aggregation."""

from __future__ import annotations

from conftest import make_settings

from opencode_telegram_controller.core.process import (
    CommandError,
    CommandRunner,
)
from opencode_telegram_controller.services import HealthMonitor
from opencode_telegram_controller.services.monitoring import (
    ERROR,
    OK,
    WARN,
    HealthCheck,
)
from opencode_telegram_controller.services.vpn import VpnStatus


class FakeSystem:
    async def check_health(self) -> HealthCheck:
        return HealthCheck("System", OK, "cpu ok")


class FakeNetwork:
    def __init__(self, gateway=True):
        self.gateway = gateway

    async def gateway_configured(self) -> bool:
        return self.gateway


class FakeVpn:
    def __init__(self, connected=True):
        self.connected = connected

    async def status(self) -> VpnStatus:
        return VpnStatus(connected=self.connected, provider="NordVPN", country="us")


class FakeDocker:
    def __init__(self, summary=None, raise_error=None):
        self._summary = summary
        self._raise_error = raise_error

    async def summary(self):
        if self._raise_error is not None:
            raise self._raise_error
        return self._summary


def make_monitor(**overrides):
    defaults = dict(
        system=FakeSystem(),
        network=FakeNetwork(),
        vpn=FakeVpn(),
        docker=FakeDocker(summary=type("S", (), {"running": 5, "total": 5, "unhealthy": 0})()),
    )
    defaults.update(overrides)
    return HealthMonitor(settings=make_settings(), runner=CommandRunner(), **defaults)


async def test_all_checks_ok():
    monitor = make_monitor()
    checks = await monitor.check()
    names = {c.name for c in checks}
    assert names == {"System", "Network", "VPN", "Docker"}
    assert all(c.status == OK for c in checks)


async def test_vpn_disconnected_is_warn():
    monitor = make_monitor(vpn=FakeVpn(connected=False))
    checks = await monitor.check()
    vpn = next(c for c in checks if c.name == "VPN")
    assert vpn.status == WARN


async def test_vpn_unconfigured_is_warn():
    monitor = make_monitor(vpn=None)
    checks = await monitor.check()
    vpn = next(c for c in checks if c.name == "VPN")
    assert vpn.status == WARN
    assert "not configured" in vpn.detail


async def test_docker_unreachable_is_error():
    monitor = make_monitor(docker=FakeDocker(raise_error=CommandError("no daemon")))
    checks = await monitor.check()
    docker = next(c for c in checks if c.name == "Docker")
    assert docker.status == ERROR


async def test_network_no_gateway_is_warn():
    monitor = make_monitor(network=FakeNetwork(gateway=False))
    checks = await monitor.check()
    network = next(c for c in checks if c.name == "Network")
    assert network.status == WARN


async def test_render_formats():
    monitor = make_monitor()
    checks = await monitor.check()
    text = monitor.render(checks)
    assert text.startswith("🩺 HEALTH")
    assert "System" in text
    assert "✅" in text


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class DegradingSystem:
    def __init__(self):
        self.calls = 0

    async def check_health(self) -> HealthCheck:
        self.calls += 1
        if self.calls == 1:
            return HealthCheck("System", OK, "cpu ok")
        return HealthCheck("System", ERROR, "cpu pegged")


async def test_proactive_alert_only_on_degradation():
    system = DegradingSystem()
    notifier = FakeNotifier()
    monitor = make_monitor(system=system, notifier=notifier)
    await monitor._run_single_check()
    assert notifier.sent == []
    await monitor._run_single_check()
    assert len(notifier.sent) == 1
    assert "System" in notifier.sent[0]
    assert "ok" in notifier.sent[0] and "error" in notifier.sent[0]
    await monitor._run_single_check()
    assert len(notifier.sent) == 1  # no repeat alert for same state


async def test_proactive_alert_ignores_improvement():
    system = DegradingSystem()
    notifier = FakeNotifier()
    monitor = make_monitor(system=system, notifier=notifier)
    await monitor._run_single_check()  # baseline OK
    await monitor._run_single_check()  # degrades to ERROR -> alert
    assert len(notifier.sent) == 1
    # Force a better state; on the next loop there is nothing new to alert.
    await monitor._run_single_check()
    assert len(notifier.sent) == 1
