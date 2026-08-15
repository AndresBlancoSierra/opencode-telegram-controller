"""Tests for the new PC Control command handlers (management, perms, security)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Audio, Chat, Message, Update, User, Video, Voice
from conftest import FakeBot, make_settings

from opencode_telegram_controller.auth import AuthorizationService
from opencode_telegram_controller.bot import AppContext, build_router
from opencode_telegram_controller.core.permissions import PermissionRegistry
from opencode_telegram_controller.notifications import NotificationManager
from opencode_telegram_controller.services.monitoring import OK, HealthCheck
from opencode_telegram_controller.services.system import (
    CpuInfo,
    FilesystemInfo,
    MemoryInfo,
    ProcessesSnapshot,
    ProcessSample,
    ResourcesInfo,
    SystemSnapshot,
)
from opencode_telegram_controller.task_manager import TaskManager

AUTHORIZED_ID = 123
READ_ONLY_ID = 456


class StubExecutor:
    def request_cancel(self, task_id: int) -> None:
        pass

    def register_completion_wait(self, task_id: int) -> asyncio.Future[str]:
        raise NotImplementedError

    def resolve_completion(self, task_id: int, text: str) -> None:
        pass


class FakeSystem:
    def __init__(self):
        self.memory = MemoryInfo(total_mb=7782, used_mb=1830, available_mb=5952)
        self.snapshot_result = SystemSnapshot(
            hostname="arrakis",
            cpu_percent=12.0,
            memory=self.memory,
            disk_usage_percent=71,
            uptime_seconds=123456,
            load_average=(0.5, 0.4, 0.3),
        )
        self.resources_result = ResourcesInfo(
            cpu=CpuInfo(percent=12.0),
            memory=self.memory,
            swap_total_mb=4096,
            swap_used_mb=128,
            load_average=(0.5, 0.4, 0.3),
            uptime_seconds=123456,
            hostname="arrakis",
        )
        self.disk_result = [
            FilesystemInfo(
                mount="/", device="/dev/nvme0n1p2", total_gb=100.0, used_gb=71.0, percent=71
            ),
            FilesystemInfo(
                mount="/home", device="/dev/nvme0n1p3", total_gb=200.0, used_gb=122.0, percent=61
            ),
        ]
        self.processes_result = ProcessesSnapshot(
            by_cpu=[ProcessSample(pid=1, name="firefox", cpu_percent=21.0, memory_mb=1200.0)],
            by_memory=[ProcessSample(pid=1, name="firefox", cpu_percent=21.0, memory_mb=1200.0)],
        )

    async def snapshot(self):
        return self.snapshot_result

    async def resources(self):
        return self.resources_result

    async def disk(self):
        return self.disk_result

    async def processes(self, limit=5):
        return self.processes_result


class FakeNetwork:
    class Status:
        def __init__(self):
            self.public_ip = "1.2.3.4"
            self.local_ip = "192.168.1.20"
            self.interface = "wlan0"
            self.gateway = "192.168.1.1"

    class Dns:
        def __init__(self):
            self.backend = "systemd-resolved"
            self.servers = ["1.1.1.1", "9.9.9.9"]
            self.search_domains = ["lan"]
            self.notes = ["systemd-resolved in use (resolvectl available)"]

    class Info:
        def __init__(self):
            self.interfaces = []

    async def status(self):
        return self.Status()

    async def dns(self):
        return self.Dns()

    async def network_info(self):
        info = self.Info()
        info.interfaces = [
            type("I", (), {"name": "wlan0", "state": "up", "ipv4": "192.168.1.20"})()
        ]
        return info

    async def gateway(self):
        return "192.168.1.1"

    async def public_ip(self):
        return "1.2.3.4"


class FakeVpn:
    name = "NordVPN"

    def __init__(self, settings):
        self.settings = settings
        self.connect_calls: list[str] = []
        self.dedicated_calls: list[str] = []
        self.reconnect_calls = 0
        self.connected = True

    async def status(self):
        from opencode_telegram_controller.services.vpn import VpnStatus

        return VpnStatus(
            connected=self.connected,
            provider="NordVPN",
            server="us100",
            country="United States",
            ip="10.5.0.2",
            kill_switch=True,
        )

    async def connect(self, target):
        self.connect_calls.append(target.country)
        self.connected = True
        return await self.status()

    async def connect_dedicated(self, server):
        self.dedicated_calls.append(server)
        return await self.status()

    async def reconnect(self):
        self.reconnect_calls += 1
        self.connected = True
        return await self.status()

    async def disconnect(self):
        self.connected = False
        return await self.status()

    async def list_countries(self):
        return [c.lower() for c in self.settings.vpn_countries]

    def resolve_target(self, token):
        from opencode_telegram_controller.services.vpn import VpnTarget

        normalized = token.strip().lower()
        allowed = {c.lower() for c in self.settings.vpn_countries}
        if normalized not in allowed:
            raise ValueError(f"Unknown country {token!r}. Allowed: {', '.join(sorted(allowed))}")
        return VpnTarget(country=normalized)

    def validate_server(self, server):
        from opencode_telegram_controller.services.vpn import VpnError

        if not server:
            raise VpnError("No dedicated VPN server is configured (OTC_VPN_DEDICATED_SERVER)")
        if not all(ch.isalnum() or ch in ".-" for ch in server):
            raise VpnError("Invalid dedicated VPN server name")
        return server


class FakeMonitor:
    async def check(self):
        return [HealthCheck("System", OK, "cpu ok")]

    def render(self, checks):
        return "🩺 HEALTH\n✅ System  cpu ok"


class FakeDocker:
    def __init__(self):
        from opencode_telegram_controller.services.docker import (
            DockerContainer,
            DockerSummary,
        )

        self.restarts: list[str] = []
        self.log_calls: list[tuple[str, int]] = []
        self.summary_result = DockerSummary(total=2, running=1, stopped=1, unhealthy=0)
        self.containers_result = [
            DockerContainer(
                id="a1",
                name="web",
                image="nginx",
                status="Up 3 hours",
                state="running",
                health="healthy",
            ),
            DockerContainer(
                id="b2",
                name="db",
                image="postgres",
                status="Exited (0)",
                state="exited",
                health=None,
            ),
        ]

    async def summary(self):
        return self.summary_result

    async def containers(self):
        return self.containers_result

    async def restart(self, name):
        self.validate_name(name)
        self.restarts.append(name)

    async def logs(self, name, lines=200):
        self.validate_name(name)
        self.log_calls.append((name, lines))
        return f"[{name}] hello\n"

    def validate_name(self, name):
        from opencode_telegram_controller.services.docker import DockerError

        if ";" in name or "/" in name or "$" in name:
            raise DockerError(f"Invalid container name: {name!r}")
        return name.strip()

    def is_allowed(self, name):
        return True


class FakeDesktop:
    def __init__(self):
        self.lock_calls = 0

    async def screenshot(self):
        import os
        import tempfile
        from pathlib import Path

        from opencode_telegram_controller.services.desktop import ScreenshotResult

        fd, path = tempfile.mkstemp(suffix=".png")
        os.write(fd, b"fake-png")
        os.close(fd)
        return ScreenshotResult(path=Path(path), size_bytes=8)

    async def windows(self, limit=20):
        from opencode_telegram_controller.services.desktop import WindowInfo

        return [
            WindowInfo(title="Terminal", class_name="kitty", workspace=1, pid=100),
            WindowInfo(title="Browser", class_name="firefox", workspace=2, pid=200),
        ]

    async def lock(self):
        self.lock_calls += 1


class FakePower:
    def __init__(self):
        self.performed = []

    async def perform(self, confirmation):
        from opencode_telegram_controller.services.power import PowerActionResult

        self.performed.append(confirmation.action)
        return PowerActionResult(
            action=confirmation.action, executed=True, detail=confirmation.action
        )


class FakeMedia:
    def __init__(self):
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp())
        self.played_audio: list = []
        self.played_video: list = []
        self.photos = 0
        self.recordings: list[int] = []

    async def download_to_temp(self, bot, file_id, suffix):
        path = self.tmp / f"play-{file_id}{suffix}"
        path.write_bytes(b"media")
        return path

    async def play_audio(self, path):
        self.played_audio.append(path)

    async def play_video(self, path):
        self.played_video.append(path)

    async def photo(self):
        from opencode_telegram_controller.services.media import PhotoResult

        self.photos += 1
        path = self.tmp / "photo.jpg"
        path.write_bytes(b"\xff\xd8jpeg")
        return PhotoResult(path=path)

    async def record_mic(self, seconds):
        from opencode_telegram_controller.services.media import RecordingResult

        self.recordings.append(seconds)
        path = self.tmp / "record.mp3"
        path.write_bytes(b"id3")
        return RecordingResult(path=path)


class FakeStream:
    def __init__(self):
        self.started: list[int] = []
        self.stopped: list[int] = []

    async def start(self, chat_id):
        self.started.append(chat_id)

    async def stop(self, chat_id):
        self.stopped.append(chat_id)
        return chat_id in self.started

    def is_streaming(self, chat_id):
        return chat_id in self.started


def make_ctx(repo, registry, *, user_ids=None, read_only_ids=()):
    settings = make_settings(default_project="A")
    settings.vpn_countries = ["us", "germany"]
    notifier = NotificationManager(FakeBot(), [AUTHORIZED_ID])
    executor = StubExecutor()
    manager = TaskManager(
        repo=repo, registry=registry, notifier=notifier, executor=executor, settings=settings
    )
    auth = AuthorizationService(list(user_ids or [AUTHORIZED_ID]), on_security_event=lambda t: None)
    permissions = PermissionRegistry(
        admin_user_ids=list(user_ids or [AUTHORIZED_ID]), read_only_user_ids=list(read_only_ids)
    )
    fake_system = FakeSystem()
    fake_vpn = FakeVpn(settings)
    fake_network = FakeNetwork()
    fake_monitor = FakeMonitor()
    fake_docker = FakeDocker()
    fake_desktop = FakeDesktop()
    fake_power = FakePower()
    fake_media = FakeMedia()
    fake_stream = FakeStream()
    from opencode_telegram_controller.core.confirmation import ConfirmationManager

    confirmations = ConfirmationManager(timeout_seconds=60)
    return AppContext(
        settings=settings,
        repo=repo,
        registry=registry,
        auth=auth,
        manager=manager,
        executor=executor,
        worker=None,
        notifier=notifier,
        started_at=datetime.now(UTC),
        system=fake_system,
        network=fake_network,
        vpn=fake_vpn,
        docker=fake_docker,
        desktop=fake_desktop,
        power=fake_power,
        media=fake_media,
        stream=fake_stream,
        monitoring=fake_monitor,
        permissions=permissions,
        confirmations=confirmations,
    )


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def fake_call(self, method, request_timeout=None):
        messages.append(method.model_dump())
        return None

    monkeypatch.setattr(Bot, "__call__", fake_call)
    return messages


@pytest.fixture
def bot():
    return Bot(token="123:abc", default=DefaultBotProperties())


async def feed(bot, dp, *, user_id=AUTHORIZED_ID, text=None):
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Tester"),
        text=text,
    )
    await dp.feed_update(bot, Update(update_id=1, message=message))


async def build_dp(ctx) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(ctx))
    return dp


async def test_status_shows_dashboard(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/status")
    text = sent[-1]["text"]
    assert "SYSTEM STATUS" in text
    assert "CPU" in text and "12%" in text
    assert "Active project: A" in text
    assert "Running tasks: 0" in text


async def test_resources(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/resources")
    text = sent[-1]["text"]
    assert "RESOURCES" in text
    assert "arrakis" in text
    assert "Swap" in text


async def test_disk(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/disk")
    text = sent[-1]["text"]
    assert "FILESYSTEM" in text
    assert "/" in text
    assert "71" in text or "61" in text


async def test_processes(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/processes")
    text = sent[-1]["text"]
    assert "TOP PROCESSES" in text
    assert "firefox" in text


async def test_health(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/health")
    text = sent[-1]["text"]
    assert "HEALTH" in text
    assert "System" in text


async def test_ip(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/ip")
    text = sent[-1]["text"]
    assert "PUBLIC IP" in text
    assert "1.2.3.4" in text
    assert "wlan0" in text


async def test_dns(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/dns")
    text = sent[-1]["text"]
    assert "DNS" in text
    assert "1.1.1.1" in text


async def test_network(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/network")
    text = sent[-1]["text"]
    assert "NETWORK" in text
    assert "wlan0" in text


async def test_read_only_user_can_read_but_not_control(repo, registry, sent, bot):
    ctx = make_ctx(
        repo, registry, read_only_ids=[READ_ONLY_ID], user_ids=[AUTHORIZED_ID, READ_ONLY_ID]
    )
    dp = await build_dp(ctx)
    # READ commands are allowed for a read-only user.
    await feed(bot, dp, user_id=READ_ONLY_ID, text="/resources")
    assert "RESOURCES" in sent[-1]["text"]
    assert "Permission denied" not in sent[-1]["text"]


async def test_malicious_arguments_are_ignored(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    for malicious, marker in (
        ("/processes; rm -rf /", None),
        ("/resources $(whoami)", "RESOURCES"),
        ("/disk foo;cat /etc/passwd", "FILESYSTEM"),
        ("/dns --connect evil", "DNS"),
        ("/processes", "TOP PROCESSES"),
    ):
        await feed(bot, dp, text=malicious)
        if marker:
            assert marker in sent[-1]["text"]
    # every response is a normal capability reply, no injection executed:
    for message in sent:
        assert "Permission denied" not in message["text"]


async def test_vpn_menu_lists_countries(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    ctx.vpn.connected = False
    ctx.settings.vpn_dedicated_server = "us4nord.example"
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/vpn")
    text = sent[-1]["text"]
    assert "🔐 VPN" in text
    assert "DISCONNECTED" in text
    assert "/vpn us" in text
    assert "/vpn germany" in text
    assert "/vpn_dedicated" in text


async def test_vpn_connect_valid_country(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/vpn germany")
    text = sent[-1]["text"]
    assert "CONNECTED" in text
    assert ctx.vpn.connect_calls == ["germany"]


async def test_vpn_connect_unknown_country_is_rejected(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/vpn github.com; rm -rf /")
    text = sent[-1]["text"]
    assert "Unknown country" in text
    assert ctx.vpn.connect_calls == []


async def test_vpn_status_shows_public_ip(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/vpn_status")
    text = sent[-1]["text"]
    assert "Ip pública" in text
    assert "1.2.3.4" in text


async def test_vpn_dedicated_connects(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    ctx.settings.vpn_dedicated_server = "us4nord.example"
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/vpn_dedicated")
    text = sent[-1]["text"]
    assert "CONNECTED" in text
    assert ctx.vpn.dedicated_calls == ["us4nord.example"]


async def test_vpn_dedicated_unconfigured_is_rejected(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    ctx.settings.vpn_dedicated_server = ""
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/vpn_dedicated")
    text = sent[-1]["text"]
    assert "OTC_VPN_DEDICATED_SERVER" in text
    assert ctx.vpn.dedicated_calls == []


async def test_docker_summary(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/docker")
    text = sent[-1]["text"]
    assert "DOCKER" in text
    assert "2" in text
    assert "1/2 running" in text


async def test_docker_status_list(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/docker_status")
    text = sent[-1]["text"]
    assert "DOCKER STATUS" in text
    assert "web" in text
    assert "db" in text


async def test_docker_restart_valid(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/docker_restart web")
    text = sent[-1]["text"]
    assert "restarted" in text
    assert ctx.docker.restarts == ["web"]


async def test_docker_restart_injection_rejected(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/docker_restart web;rm -rf /")
    text = sent[-1]["text"]
    assert "Invalid container name" in text
    assert ctx.docker.restarts == []


async def test_docker_restart_requires_name(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/docker_restart")
    text = sent[-1]["text"]
    assert "Usage" in text
    assert ctx.docker.restarts == []


async def test_docker_logs_valid(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/docker_logs web 50")
    text = sent[-1]["text"]
    assert "hello" in text
    assert ctx.docker.log_calls == [("web", 50)]


async def test_docker_logs_injection_rejected(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/docker_logs evil/../etc")
    text = sent[-1]["text"]
    assert "Invalid container name" in text
    assert ctx.docker.log_calls == []


async def test_windows_lists(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/windows")
    text = sent[-1]["text"]
    assert "WINDOWS" in text
    assert "Terminal" in text
    assert "firefox" in text


async def test_lock(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/lock")
    text = sent[-1]["text"]
    assert "locked" in text
    assert ctx.desktop.lock_calls == 1


async def test_screenshot(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/screenshot")
    photo = sent[-1]
    assert photo["photo"] is not None
    assert "Screenshot" in photo["caption"]


async def test_power_requires_confirmation(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/reboot")
    text = sent[-1]["text"]
    assert "/confirm_reboot" in text
    assert ctx.power.performed == []


async def test_power_confirm_executes(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/sleep")
    await feed(bot, dp, text="/confirm_sleep")
    text = sent[-1]["text"]
    assert "executed" in text
    assert ctx.power.performed == ["sleep"]


async def test_power_confirm_without_request_is_rejected(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/confirm_shutdown")
    text = sent[-1]["text"]
    assert "No pending confirmation" in text
    assert ctx.power.performed == []


async def test_power_dismiss_cancels(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/reboot")
    await feed(bot, dp, text="/dismiss")
    text = sent[-1]["text"]
    assert "Cancelled 1" in text
    assert ctx.power.performed == []


async def test_dismiss_without_pending(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/dismiss")
    text = sent[-1]["text"]
    assert "No pending confirmations" in text


async def test_read_only_user_cannot_destructive(repo, registry, sent, bot):
    ctx = make_ctx(
        repo, registry, read_only_ids=[READ_ONLY_ID], user_ids=[AUTHORIZED_ID, READ_ONLY_ID]
    )
    dp = await build_dp(ctx)
    await feed(bot, dp, user_id=READ_ONLY_ID, text="/reboot")
    text = sent[-1]["text"]
    assert "Permission denied" in text
    assert ctx.power.performed == []


async def test_start_shows_pc_dashboard(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/start")
    text = sent[-1]["text"]
    assert "PC Control Bot" in text
    assert "/status" in text
    assert "/reboot" in text


async def test_help_lists_pc_commands(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/help")
    text = sent[-1]["text"]
    assert "OpenCode Telegram Controller" in text
    for marker in (
        "/docker",
        "/vpn",
        "/screenshot",
        "/reboot",
        "/health",
        "/windows",
        "/vpn_change",
        "/photo",
        "/record_mic",
        "/stream",
    ):
        assert marker in text


async def test_vpn_change_reconnects(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/vpn_change")
    assert ctx.vpn.reconnect_calls == 1
    assert "🔐 VPN" in sent[-1]["text"]


async def test_cambiar_alias_reconnects(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/cambiar")
    assert ctx.vpn.reconnect_calls == 1


async def test_photo(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/photo")
    assert ctx.media.photos == 1
    assert "photo" in sent[-1]


async def test_record_mic(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/record_mic 5")
    assert ctx.media.recordings == [5]
    assert "audio" in sent[-1]


async def test_record_mic_rejects_invalid_length(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/record_mic 999")
    assert ctx.media.recordings == []
    assert "between 1 and" in sent[-1]["text"]


async def test_audio_autoplay(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=AUTHORIZED_ID, type="private"),
        from_user=User(id=AUTHORIZED_ID, is_bot=False, first_name="Tester"),
        audio=Audio(file_id="A1", duration=3, file_unique_id="ua1"),
    )
    await dp.feed_update(bot, Update(update_id=1, message=message))
    assert len(ctx.media.played_audio) == 1
    assert "Reproduciendo" in sent[-1]["text"]


async def test_voice_autoplay(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=AUTHORIZED_ID, type="private"),
        from_user=User(id=AUTHORIZED_ID, is_bot=False, first_name="Tester"),
        voice=Voice(file_id="V1", duration=3, file_unique_id="uv1"),
    )
    await dp.feed_update(bot, Update(update_id=1, message=message))
    assert len(ctx.media.played_audio) == 1
    assert "Reproduciendo" in sent[-1]["text"]


async def test_video_autoplay(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=AUTHORIZED_ID, type="private"),
        from_user=User(id=AUTHORIZED_ID, is_bot=False, first_name="Tester"),
        video=Video(
            file_id="V1",
            width=1280,
            height=720,
            duration=5,
            file_unique_id="uv1",
        ),
    )
    await dp.feed_update(bot, Update(update_id=1, message=message))
    assert len(ctx.media.played_video) == 1


async def test_stream_start_and_stop(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/stream")
    assert ctx.stream.started == [AUTHORIZED_ID]
    assert "Live stream started" in sent[-1]["text"]
    await feed(bot, dp, text="/stream_stop")
    assert ctx.stream.stopped == [AUTHORIZED_ID]
    assert "Live stream stopped" in sent[-1]["text"]


async def test_read_only_cannot_use_media_commands(repo, registry, sent, bot):
    ctx = make_ctx(
        repo, registry, read_only_ids=[READ_ONLY_ID], user_ids=[AUTHORIZED_ID, READ_ONLY_ID]
    )
    dp = await build_dp(ctx)
    await feed(bot, dp, user_id=READ_ONLY_ID, text="/photo")
    assert "Permission denied" in sent[-1]["text"]
    assert ctx.media.photos == 0
    await feed(bot, dp, user_id=READ_ONLY_ID, text="/stream")
    assert ctx.stream.started == []
