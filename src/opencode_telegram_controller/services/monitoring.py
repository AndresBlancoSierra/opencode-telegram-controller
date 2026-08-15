"""Health monitoring.

Aggregates checks across the other services (system, network, VPN, Docker,
disk, OpenCode) into a structured report for ``/health``. A background loop
(disabled by default) can push proactive alerts when a check degrades.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass

from ..config import Settings
from ..core.process import CommandError, CommandRunner
from ..notifications import NotificationManager

OK = "ok"
WARN = "warn"
ERROR = "error"

_STATUS_RANK = {OK: 0, WARN: 1, ERROR: 2}


def _status_rank(status: str) -> int:
    return _STATUS_RANK.get(status, 1)


@dataclass
class HealthCheck:
    name: str
    status: str
    detail: str = ""


class HealthMonitor:
    """Runs health checks and (optionally) proactive alerts."""

    def __init__(
        self,
        *,
        settings: Settings,
        runner: CommandRunner,
        system,
        network,
        vpn,
        docker,
        notifier: NotificationManager | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.system = system
        self.network = network
        self.vpn = vpn
        self.docker = docker
        self.notifier = notifier
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last: dict[str, str] = {}

    async def check(self) -> list[HealthCheck]:
        checks: list[HealthCheck] = []

        try:
            checks.append(await self.system.check_health())
        except Exception:
            checks.append(HealthCheck("System", ERROR, "check failed"))

        try:
            configured = await self.network.gateway_configured()
            status = OK if configured else WARN
            detail = "gateway: up" if configured else "no default route"
            checks.append(HealthCheck("Network", status, detail))
        except Exception:
            checks.append(HealthCheck("Network", ERROR, "unreachable"))

        if self.vpn is not None:
            try:
                vpn_status = await self.vpn.status()
                checks.append(
                    HealthCheck(
                        "VPN",
                        OK if vpn_status.connected else WARN,
                        vpn_status.label,
                    )
                )
            except Exception:
                checks.append(HealthCheck("VPN", ERROR, "unreachable"))
        else:
            checks.append(HealthCheck("VPN", WARN, "not configured"))

        try:
            summary = await self.docker.summary()
            status = OK if summary.unhealthy == 0 and summary.running == summary.total else WARN
            checks.append(
                HealthCheck("Docker", status, f"{summary.running}/{summary.total} running")
            )
        except CommandError:
            checks.append(HealthCheck("Docker", ERROR, "daemon unreachable"))
        except Exception:
            checks.append(HealthCheck("Docker", WARN, "not available"))

        return checks

    @staticmethod
    def render(checks: list[HealthCheck]) -> str:
        if not checks:
            return "🩺 HEALTH\n\nNo checks configured."
        lines = ["🩺 HEALTH"]
        for check in checks:
            emoji = {OK: "✅", WARN: "⚠️", ERROR: "❌"}.get(check.status, "❓")
            lines.append(f"{emoji} {check.name:<10} {check.detail}")
        return "\n".join(lines)

    def enabled(self) -> bool:
        return self.settings.health_check_interval_seconds > 0

    async def start(self) -> None:
        if not self.enabled() or self._task is not None:
            return
        self._stop.clear()
        self._last.clear()
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_loop_done)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        with asyncio.timeout(3):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        interval = self.settings.health_check_interval_seconds
        while not self._stop.is_set():
            with suppress(Exception):
                await self._run_single_check()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _run_single_check(self) -> None:
        checks = await self.check()
        if self.notifier is None:
            return
        for check in checks:
            previous = self._last.get(check.name)
            self._last[check.name] = check.status
            if previous is None or previous == check.status:
                continue
            if _status_rank(check.status) > _status_rank(previous):
                await self.notifier.send(
                    f"🩺 <b>ALERT</b> {check.name}: {previous} → {check.status}\n{check.detail}"
                )

    def _on_loop_done(self, task: asyncio.Task) -> None:  # pragma: no cover
        if not task.cancelled() and task.exception() is not None:
            self.log_exception(task.exception())

    def log_exception(self, exc: BaseException) -> None:  # pragma: no cover
        from loguru import logger

        logger.warning("Health monitor loop error: {}", exc)
