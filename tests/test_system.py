"""Tests for the SystemManager and its pure parsers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opencode_telegram_controller.services import SystemManager
from opencode_telegram_controller.services.system import (
    cpu_percent_delta,
    memory_from_meminfo,
    parse_cpu_sample,
    parse_loadavg,
    parse_meminfo,
    parse_mounts,
    parse_ps_output,
    parse_uptime_seconds,
)


def make_manager(monkeypatch, *, files=None, ps_output=None, disk_usage=None):
    from opencode_telegram_controller.config import Settings
    from opencode_telegram_controller.core.process import CommandRunner

    settings = Settings(telegram_bot_token="x", allowed_user_ids=[1])
    manager = SystemManager(settings=settings, runner=CommandRunner())

    async def fake_read(path: str) -> str:
        if files is None:
            raise AssertionError(f"unexpected read {path}")
        return files[path]

    async def no_sleep(_seconds):
        return None

    manager._read = fake_read  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    if ps_output is not None:

        async def fake_run(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=ps_output, stderr="")

        manager.runner.run = fake_run  # type: ignore[method-assign]

    if disk_usage is not None:
        import opencode_telegram_controller.services.system as system_mod

        monkeypatch.setattr(system_mod.shutil, "disk_usage", disk_usage)
    return manager


SAMPLE_BEFORE = "cpu  1000 0 500 8000 150 0 0 0 0 0\ncpu0 500 0 250 4000 75 0 0 0 0 0\n"
SAMPLE_AFTER = "cpu  1200 0 700 8050 180 0 0 0 0 0\ncpu0 600 0 350 4025 90 0 0 0 0 0\n"


async def test_cpu_percent_delta():
    before = parse_cpu_sample(SAMPLE_BEFORE)
    after = parse_cpu_sample(SAMPLE_AFTER)
    assert parse_cpu_sample("cpu0 ...") == {}
    assert 0.0 <= cpu_percent_delta(before, after) <= 100.0
    total_before = sum(before.values())
    total_after = sum(after.values())
    busy = (total_after - total_before) - (
        (after["idle"] + after["iowait"]) - (before["idle"] + before["iowait"])
    )
    expected = busy / (total_after - total_before) * 100
    assert cpu_percent_delta(before, after) == pytest.approx(expected)


async def test_cpu_percent_zero_on_no_delta():
    sample = parse_cpu_sample(SAMPLE_BEFORE)
    assert cpu_percent_delta(sample, sample) == 0.0


MEMINFO = """MemTotal:       8000000 kB
MemFree:        1000000 kB
MemAvailable:   3000000 kB
Buffers:         200000 kB
Cached:         1500000 kB
SwapTotal:      1000000 kB
SwapFree:        500000 kB
"""


async def test_memory_from_meminfo_with_available():
    mem = memory_from_meminfo(parse_meminfo(MEMINFO))
    assert mem.total_mb == 8000000 // 1024
    assert mem.available_mb == 3000000 // 1024
    assert mem.used_mb == (8000000 - 3000000) // 1024


async def test_memory_fallback_without_available():
    meminfo = parse_meminfo(MEMINFO)
    del meminfo["MemAvailable"]
    mem = memory_from_meminfo(meminfo)
    free_plus = meminfo["MemFree"] + meminfo["Buffers"] + meminfo["Cached"]
    assert mem.available_mb == free_plus // 1024


async def test_loadavg_and_uptime_parsers():
    assert parse_loadavg("0.30 0.15 0.10 2/512 12345\n") == (0.30, 0.15, 0.10)
    assert parse_uptime_seconds("86400.50 500000.00\n") == 86400


MOUNTS = """/dev/nvme0n1p2 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid 0 0
tmpfs /dev/shm tmpfs rw 0 0
/dev/sda1 /srv/media ext4 rw 0 0
"""


async def test_parse_mounts_filters_pseudo_fs():
    mounts = parse_mounts(MOUNTS)
    assert ("/dev/nvme0n1p2", "/", "ext4") in mounts
    assert ("/dev/sda1", "/srv/media", "ext4") in mounts
    for _device, _, fstype in mounts:
        assert fstype not in {"proc", "tmpfs"}


PS_OUTPUT = """12345 firefox 21.0 5.5
5678 docker-proxy 0.2 0.1
9999 test daemon-with-spaces 12.3 8.0
"""


async def test_parse_ps_output():
    samples = parse_ps_output(PS_OUTPUT)
    assert len(samples) == 3
    first = samples[0]
    assert first.pid == 12345
    assert first.name == "firefox"
    assert first.cpu_percent == 21.0
    long_name = [s for s in samples if s.pid == 9999][0]
    assert long_name.name == "test daemon-with-spaces"
    assert long_name.memory_mb == 8.0


async def test_resources_builds_snapshot(monkeypatch):
    files = {
        "/proc/stat": SAMPLE_BEFORE,
        "/proc/meminfo": MEMINFO,
        "/proc/loadavg": "0.30 0.15 0.10 2/512 12345\n",
        "/proc/uptime": "500.00 300.00\n",
    }
    manager = make_manager(monkeypatch, files=files)
    resources = await manager.resources()
    assert resources.hostname
    assert resources.memory.total_mb > 0
    assert resources.uptime_seconds == 500
    assert resources.load_average == (0.30, 0.15, 0.10)
    assert resources.swap_total_mb > 0


async def test_disk_lists_unique_mounts(monkeypatch):
    files = {"/proc/mounts": MOUNTS}

    def fake_disk_usage(path):
        return SimpleNamespace(total=100 * 1024**3, used=50 * 1024**3)

    manager = make_manager(monkeypatch, files=files, disk_usage=fake_disk_usage)
    infos = await manager.disk()
    assert {info.mount for info in infos} == {"/", "/srv/media"}
    assert all(info.percent == 50 for info in infos)


async def test_snapshot_summarizes(monkeypatch):
    files = {
        "/proc/stat": SAMPLE_BEFORE,
        "/proc/meminfo": MEMINFO,
        "/proc/loadavg": "0.10 0.10 0.10\n",
        "/proc/uptime": "3600.0\n",
        "/proc/mounts": MOUNTS,
    }

    def fake_disk_usage(path):
        return SimpleNamespace(total=100 * 1024**3, used=25 * 1024**3)

    manager = make_manager(monkeypatch, files=files, disk_usage=fake_disk_usage)
    snapshot = await manager.snapshot()
    assert snapshot.uptime_seconds == 3600
    assert snapshot.disk_usage_percent == 25
    assert snapshot.memory.total_mb > 0


async def test_processes_uses_ps_and_sorts(monkeypatch):
    files = {
        "/proc/meminfo": MEMINFO,
        "/proc/stat": SAMPLE_BEFORE,
        "/proc/loadavg": "0.10 0.10 0.10\n",
        "/proc/uptime": "1000.0\n",
    }
    manager = make_manager(monkeypatch, files=files, ps_output=PS_OUTPUT)
    snapshots = await manager.processes(limit=2)
    assert len(snapshots.by_cpu) == 2
    assert snapshots.by_cpu[0].name == "firefox"
    assert snapshots.by_memory[0].pid == 9999
    assert snapshots.by_memory[0].memory_mb > snapshots.by_cpu[0].memory_mb
