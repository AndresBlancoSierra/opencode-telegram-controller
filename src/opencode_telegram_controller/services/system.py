"""System capability: resources, disk, processes and status snapshots.

Data is read from the OS (``/proc``, ``shutil.disk_usage``) instead of parsing
free-form tool output. Pure parsing helpers take raw text so tests stay
deterministic without touching real proc files.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
from dataclasses import dataclass, field

from ..config import Settings
from ..core.process import CommandRunner
from .monitoring import OK, WARN, HealthCheck

_CPU_SAMPLE_DELAY = 0.2
_DISK_WARN_PERCENT = 80
_CPU_WARN_PERCENT = 90
_LOAD_WARN_NORMALIZED = 2.0
_CPU_KEYS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
_MOUNT_SKIP_FSTYPES = frozenset(
    {
        "proc",
        "sysfs",
        "devpts",
        "devtmpfs",
        "securityfs",
        "tmpfs",
        "cgroup2",
        "cgroup",
        "pstore",
        "bpf",
        "autofs",
        "fusectl",
        "configfs",
        "debugfs",
        "tracefs",
        "hugetlbfs",
        "mqueue",
        "binfmt_misc",
    }
)


@dataclass
class CpuInfo:
    percent: float

    @property
    def label(self) -> str:
        return f"{self.percent:.0f}%"


@dataclass
class MemoryInfo:
    total_mb: int
    used_mb: int
    available_mb: int

    @property
    def percent(self) -> float:
        return (self.used_mb / self.total_mb * 100) if self.total_mb else 0.0


@dataclass
class FilesystemInfo:
    mount: str
    device: str
    total_gb: float
    used_gb: float
    percent: int


@dataclass
class ProcessSample:
    pid: int
    name: str
    cpu_percent: float
    memory_mb: float


@dataclass
class ProcessesSnapshot:
    by_cpu: list[ProcessSample] = field(default_factory=list)
    by_memory: list[ProcessSample] = field(default_factory=list)


@dataclass
class ResourcesInfo:
    cpu: CpuInfo
    memory: MemoryInfo
    swap_total_mb: int
    swap_used_mb: int
    load_average: tuple[float, float, float]
    uptime_seconds: int
    hostname: str


@dataclass
class SystemSnapshot:
    hostname: str
    cpu_percent: float
    memory: MemoryInfo
    disk_usage_percent: int
    uptime_seconds: int
    load_average: tuple[float, float, float]


# --- pure parsers (tested directly, no I/O) ----------------------------


def parse_cpu_sample(text: str) -> dict[str, int]:
    """Parse the aggregate ``cpu`` line of /proc/stat into per-state ticks."""
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        fields = line.split()
        return {key: int(fields[i + 1]) for i, key in enumerate(_CPU_KEYS)}
    return {}


def cpu_percent_delta(before: dict[str, int], after: dict[str, int]) -> float:
    """Compute CPU usage percent between two /proc/stat samples."""
    total_before = sum(before.values())
    total_after = sum(after.values())
    delta = total_after - total_before
    if delta <= 0:
        return 0.0
    idle_before = before.get("idle", 0) + before.get("iowait", 0)
    idle_after = after.get("idle", 0) + after.get("iowait", 0)
    idle_delta = idle_after - idle_before
    busy = delta - idle_delta
    return max(0.0, min(100.0, busy / delta * 100))


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo into a key -> kB mapping."""
    result: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result[parts[0].rstrip(":")] = int(parts[1])
    return result


def memory_from_meminfo(meminfo: dict[str, int]) -> MemoryInfo:
    total_kb = meminfo.get("MemTotal", 0)
    available_kb = meminfo.get("MemAvailable")
    if available_kb is None:
        free = meminfo.get("MemFree", 0)
        buffers = meminfo.get("Buffers", 0)
        cached = meminfo.get("Cached", 0)
        available_kb = free + buffers + cached
    return MemoryInfo(
        total_mb=total_kb // 1024,
        available_mb=available_kb // 1024,
        used_mb=max(0, (total_kb - available_kb) // 1024),
    )


def parse_loadavg(text: str) -> tuple[float, float, float]:
    parts = text.split()
    return tuple(float(x) for x in parts[:3])  # type: ignore[return-value]


def parse_uptime_seconds(text: str) -> int:
    return int(float(text.split()[0]))


def parse_mounts(text: str) -> list[tuple[str, str, str]]:
    """Parse /proc/mounts into (device, mount_point, fstype) tuples."""
    mounts: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fstype = parts[0], parts[1], parts[2]
        if fstype in _MOUNT_SKIP_FSTYPES:
            continue
        mounts.append((device, mount, fstype))
    return mounts


def parse_ps_output(text: str) -> list[ProcessSample]:
    """Parse ``ps -eo pid=,comm=,pcpu=,pmem=`` output."""
    samples: list[ProcessSample] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            cpu = float(parts[-2])
            mem_percent = float(parts[-1])
        except ValueError:
            continue
        name = " ".join(parts[1:-2]) or "?"
        samples.append(ProcessSample(pid=pid, name=name, cpu_percent=cpu, memory_mb=mem_percent))
    return samples


# --- manager ------------------------------------------------------------


class SystemManager:
    """Read-only access to machine resources, mounts and processes."""

    def __init__(self, *, settings: Settings, runner: CommandRunner) -> None:
        self.settings = settings
        self.runner = runner

    def available(self) -> bool:
        return os.path.exists("/proc/stat")

    async def _read(self, path: str) -> str:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()

    async def resources(self) -> ResourcesInfo:
        before = await self._read("/proc/stat")
        await asyncio.sleep(_CPU_SAMPLE_DELAY)
        after = await self._read("/proc/stat")
        cpu_percent = cpu_percent_delta(parse_cpu_sample(before), parse_cpu_sample(after))

        meminfo = parse_meminfo(await self._read("/proc/meminfo"))
        memory = memory_from_meminfo(meminfo)
        load = parse_loadavg(await self._read("/proc/loadavg"))
        uptime = parse_uptime_seconds(await self._read("/proc/uptime"))
        return ResourcesInfo(
            cpu=CpuInfo(percent=cpu_percent),
            memory=memory,
            swap_total_mb=meminfo.get("SwapTotal", 0) // 1024,
            swap_used_mb=meminfo.get("SwapUsed", 0) // 1024,
            load_average=load,
            uptime_seconds=uptime,
            hostname=socket.gethostname(),
        )

    async def disk(self) -> list[FilesystemInfo]:
        mounts = parse_mounts(await self._read("/proc/mounts"))
        infos: list[FilesystemInfo] = []
        seen_devices: set[str] = set()
        for device, mount, _fstype in mounts:
            if device in seen_devices:
                continue
            seen_devices.add(device)
            try:
                usage = shutil.disk_usage(mount)
            except OSError:
                continue
            if usage.total <= 0:
                continue
            infos.append(
                FilesystemInfo(
                    mount=mount,
                    device=device,
                    total_gb=round(usage.total / (1024**3), 1),
                    used_gb=round(usage.used / (1024**3), 1),
                    percent=int(usage.used / usage.total * 100),
                )
            )
        infos.sort(key=lambda f: f.percent, reverse=True)
        return infos

    async def processes(self, limit: int = 5) -> ProcessesSnapshot:
        result = await self.runner.run(
            (
                "ps",
                "-eo",
                "pid=,comm=,pcpu=,pmem=",
                "--sort=-pcpu",
            ),
            timeout=self.settings.timeout_quick_seconds,
        )
        samples = parse_ps_output(result.stdout)
        total_mb = (await self.resources()).memory.total_mb
        for sample in samples:
            sample.memory_mb = total_mb * sample.memory_mb / 100.0
        by_cpu = samples[:limit]
        by_memory = sorted(samples, key=lambda s: s.memory_mb, reverse=True)[:limit]
        return ProcessesSnapshot(by_cpu=by_cpu, by_memory=by_memory)

    async def snapshot(self) -> SystemSnapshot:
        res = await self.resources()
        disks = await self.disk()
        root = next((d for d in disks if d.mount == "/"), None)
        return SystemSnapshot(
            hostname=res.hostname,
            cpu_percent=res.cpu.percent,
            memory=res.memory,
            disk_usage_percent=root.percent if root else 0,
            uptime_seconds=res.uptime_seconds,
            load_average=res.load_average,
        )

    async def check_health(self) -> HealthCheck:
        res = await self.resources()
        issues: list[str] = []
        if res.cpu.percent >= _CPU_WARN_PERCENT:
            issues.append(f"CPU {res.cpu.percent:.0f}%")
        cpus = os.cpu_count() or 1
        if res.load_average[0] / cpus >= _LOAD_WARN_NORMALIZED:
            issues.append("high load")
        disk_checks = await self.disk()
        overfull = [d for d in disk_checks if d.percent >= _DISK_WARN_PERCENT]
        if overfull:
            issues.append(f"disk {overfull[0].mount} {overfull[0].percent}%")
        detail = (
            f"CPU {res.cpu.percent:.0f}% · load {res.load_average[0]:.2f} · "
            f"RAM {res.memory.used_mb}MB/{res.memory.total_mb}MB"
        )
        if issues:
            return HealthCheck("System", WARN, f"{detail} | {'; '.join(issues)}")
        return HealthCheck("System", OK, detail)
