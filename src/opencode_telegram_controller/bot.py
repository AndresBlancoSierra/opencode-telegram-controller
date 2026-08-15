"""Telegram bot: authorization, commands and natural-language task intake.

Uses aiogram 3 with long polling (no webhook, no exposed ports). Every update
is checked against the user allowlist before any handler runs.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import TelegramAPIServer
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from loguru import logger

from .auth import AuthorizationService
from .config import Settings
from .core.audit import AuditLogger
from .core.confirmation import ConfirmationManager
from .core.permissions import PermissionRegistry
from .formatting import (
    dashboard_text,
    format_projects_list,
    format_session_detail,
    format_sessions_list,
    format_task_detail,
    format_task_line,
    help_text,
    split_text,
)
from .models import TaskStatus
from .notifications import NotificationManager
from .projects import ProjectRegistry
from .queue_worker import QueueWorker
from .repository import TaskRepository
from .services import (
    DesktopManager,
    DockerManager,
    HealthMonitor,
    LiveStreamManager,
    MediaManager,
    NetworkManager,
    PowerManager,
    SystemManager,
    VpnManager,
)
from .task_executor import TaskExecutor
from .task_manager import SessionError, TaskError, TaskManager


@dataclass
class AppContext:
    settings: Settings
    repo: TaskRepository
    registry: ProjectRegistry
    auth: AuthorizationService
    manager: TaskManager
    executor: TaskExecutor
    worker: QueueWorker
    notifier: NotificationManager
    started_at: datetime

    # PC Control capabilities (wired in main; optional so tests stay simple)
    system: SystemManager | None = None
    network: NetworkManager | None = None
    vpn: VpnManager | None = None
    docker: DockerManager | None = None
    desktop: DesktopManager | None = None
    power: PowerManager | None = None
    media: MediaManager | None = None
    stream: LiveStreamManager | None = None
    monitoring: HealthMonitor | None = None
    audit: AuditLogger | None = None
    permissions: PermissionRegistry | None = None
    confirmations: ConfirmationManager | None = None

    @property
    def opencode(self) -> TaskManager:
        """OpenCode capability: the task manager that controls OpenCode."""
        return self.manager


class ContextMiddleware(BaseMiddleware):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self._ctx = ctx

    async def __call__(self, handler, event, data):
        data["ctx"] = self._ctx
        if isinstance(event, Message):
            kind = event.content_type or event.text
            logger.debug("message from {} ({})", event.from_user.id, kind)
        return await handler(event, data)


class AuthMiddleware(BaseMiddleware):
    def __init__(self, auth: AuthorizationService):
        super().__init__()
        self._auth = auth

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or self._auth.is_authorized(user.id):
            return await handler(event, data)
        await self._auth.handle_unauthorized(user.id, getattr(user, "username", None))
        if isinstance(event, Message):
            with suppress(TelegramAPIError):
                await event.answer("⛔ Unauthorized. This bot is private.")
        return


def build_bot(settings: Settings) -> Bot:
    api_server = TelegramAPIServer.from_base(settings.telegram_api_base)
    if settings.telegram_nameservers:
        session = _DnsOverrideSession(api=api_server, nameservers=settings.telegram_nameservers)
    else:
        session = AiohttpSession(api=api_server)
    return Bot(
        token=settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(),
    )


class _DnsOverrideSession(AiohttpSession):
    """Aiohttp session that resolves the Telegram API via custom DNS servers.

    Used to work around DNS providers (e.g. NextDNS) that block
    api.telegram.org by resolving it to 0.0.0.0.
    """

    def __init__(self, *, nameservers: list[str], **kwargs):
        super().__init__(**kwargs)
        self._connector_init["resolver"] = _build_resolver(nameservers)


def _build_resolver(nameservers: list[str]):
    try:
        from aiohttp.resolver import AsyncResolver
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "OTC_TELEGRAM_NAMESERVERS requires the 'dns' extra. "
            "Install it with: uv sync --extra dns"
        ) from exc
    return AsyncResolver(nameservers=list(nameservers))


def build_router(ctx: AppContext) -> Router:
    router = Router()
    router.message.middleware(ContextMiddleware(ctx))
    router.message.middleware(AuthMiddleware(ctx.auth))

    from .commands import desktop as desktop_commands
    from .commands import docker as docker_commands
    from .commands import media as media_commands
    from .commands import network as network_commands
    from .commands import power as power_commands
    from .commands import stream as stream_commands
    from .commands import system as system_commands
    from .commands import vpn as vpn_commands

    system_commands.register(router)
    network_commands.register(router)
    vpn_commands.register(router)
    docker_commands.register(router)
    desktop_commands.register(router)
    power_commands.register(router)
    media_commands.register(router)
    stream_commands.register(router)

    @router.message(CommandStart())
    async def on_start(message: Message, ctx: AppContext):
        if (
            await ctx.repo.get_active_project(message.from_user.id) is None
            and ctx.settings.default_project
        ):
            with suppress(Exception):  # pragma: no cover - defensive
                await ctx.repo.set_active_project(
                    message.from_user.id, ctx.settings.default_project
                )
        await message.answer(dashboard_text(), parse_mode="HTML")

    @router.message(Command("help"))
    async def on_help(message: Message, ctx: AppContext):
        await message.answer(help_text(), parse_mode="HTML")

    @router.message(Command("projects"))
    async def on_projects(message: Message, ctx: AppContext):
        projects = list(ctx.registry.projects.values())
        if not projects:
            await message.answer("No projects configured.")
            return
        active = await ctx.manager.active_project(message.from_user.id)
        await message.answer(format_projects_list(projects, active.name if active else None))

    @router.message(Command("use"))
    async def on_use(message: Message, ctx: AppContext):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Usage: /use <project name>\nSee /projects for the available names."
            )
            return
        name = parts[1].strip()
        try:
            project = await ctx.manager.set_active_project(message.from_user.id, name)
        except KeyError:
            await message.answer(f"Unknown or disabled project: {name}")
            return
        await message.answer(f"✅ Active project set to: {project.name}")

    @router.message(Command("tasks"))
    async def on_tasks(message: Message, ctx: AppContext):
        parts = message.text.split(maxsplit=1)
        show_all = bool(parts[1:]) and parts[1].strip().lower() == "all"
        session = None if show_all else await ctx.manager.active_session(message.from_user.id)
        if session is not None:
            tasks = await ctx.repo.list_tasks_by_session(session.id, limit=10)
            header = f"🗂️ Messages of session #{session.id}:\n"
        else:
            tasks = await ctx.repo.list_tasks(limit=10)
            header = "📋 Recent tasks:\n"
        if not tasks:
            if session is not None:
                await message.answer(f"No messages in session #{session.id} yet. Send one!")
            else:
                await message.answer(
                    "No active session and no tasks yet.\n\nUse /new to start a session."
                )
            return
        lines = [header]
        lines.extend(format_task_line(t) for t in tasks)
        lines.append("")
        lines.append("Use /task <id> for details.")
        await message.answer("\n".join(lines))

    @router.message(Command("new"))
    async def on_new(message: Message, ctx: AppContext):
        parts = message.text.split(maxsplit=1)
        title = None
        if len(parts) > 1 and parts[1].strip():
            title = parts[1].strip()[:80]
        try:
            session = await ctx.manager.new_session(message.from_user.id, title=title)
        except SessionError as exc:
            await message.answer(str(exc))
            return
        text = "🧠 New OpenCode session\n\n"
        if session.opencode_session_id:
            text += f"ID: {session.opencode_session_id}\n"
        text += f"Internal ID: #{session.id}\n"
        text += f"Project: {session.project_id}\n"
        if session.title:
            text += f"Title: {session.title}\n"
        text += (
            "\nSession is now active. Send a message to start chatting.\n"
            "The OpenCode session ID is assigned on the first message."
        )
        await message.answer(text)

    @router.message(Command("history"))
    async def on_history(message: Message, ctx: AppContext):
        sessions = await ctx.manager.list_sessions(message.from_user.id, limit=25)
        active = await ctx.manager.active_session(message.from_user.id)
        await message.answer(format_sessions_list(sessions, active.id if active else None))

    @router.message(Command("continue"))
    async def on_continue(message: Message, ctx: AppContext):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("Usage: /continue <session-id>\nSee /history for your sessions.")
            return
        ref = parts[1].strip()
        try:
            session = await ctx.manager.continue_session(message.from_user.id, ref)
        except SessionError as exc:
            await message.answer(str(exc))
            return
        text = "✅ OpenCode session restored\n\n"
        if session.opencode_session_id:
            text += f"ID: {session.opencode_session_id}\n"
        text += f"Internal ID: #{session.id}\n"
        text += f"Project: {session.project_id}\n"
        if session.title:
            text += f"Title: {session.title}\n"
        text += "\nContinue chatting."
        await message.answer(text)

    @router.message(Command("current"))
    async def on_current(message: Message, ctx: AppContext):
        session = await ctx.manager.active_session(message.from_user.id)
        if session is None:
            await message.answer(
                "No active session.\n\nUse /new to start one or /history to see past sessions."
            )
            return
        task_count = await ctx.manager.session_task_count(session.id)
        await message.answer(format_session_detail(session, task_count))

    @router.message(Command("task"))
    async def on_task(message: Message, ctx: AppContext):
        task_id = _parse_task_id(message)
        if task_id is None:
            await message.answer("Usage: /task <id>")
            return
        task = await ctx.repo.get_task(task_id)
        if task is None:
            await message.answer(f"No task with id {task_id}.")
            return
        text = format_task_detail(task)
        if task.error:
            text += f"\n\nError: {task.error}"
        if task.commit_created:
            text += f"\n\nCommit created: {task.commit_created}"
        for chunk in split_text(text):
            await message.answer(chunk)

    @router.message(Command("cancel"))
    async def on_cancel(message: Message, ctx: AppContext):
        task_id = _parse_task_id(message)
        if task_id is None:
            running = await ctx.repo.list_tasks(status=TaskStatus.RUNNING, limit=5)
            candidates = [t for t in running if t.user_id == message.from_user.id]
            if not candidates:
                await message.answer("Usage: /cancel <id>\nThere is no running task to cancel.")
                return
            task_id = candidates[0].id
        try:
            await ctx.manager.cancel_task(message.from_user.id, task_id)
        except TaskError as exc:
            await message.answer(str(exc))
            return
        await message.answer(f"🛑 Cancellation requested for task #{task_id}.")

    @router.message(Command("logs"))
    async def on_logs(message: Message, ctx: AppContext):
        task_id = _parse_task_id(message)
        if task_id is None:
            recent = await ctx.repo.list_tasks(limit=1)
            task = recent[0] if recent else None
            if task is None:
                await message.answer("No tasks yet.")
                return
        else:
            task = await ctx.repo.get_task(task_id)
            if task is None:
                await message.answer(f"No task with id {task_id}.")
                return
        if not task.log_tail:
            await message.answer(f"No log output captured for task #{task.id}.")
            return
        header = f"📄 Logs for task #{task.id}:\n"
        body = task.log_tail[-3000:]
        await message.answer(header + body)

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_natural_language(message: Message, ctx: AppContext):
        user_id = message.from_user.id
        session = await ctx.manager.active_session(user_id)
        if session is None:
            await message.answer(
                "No active OpenCode session.\n\n"
                "Use /new to start one, /continue <id> to resume a past session, "
                "or /use <name> to select a project first."
            )
            return
        try:
            reply = await ctx.manager.send_message(user_id, session, message.text)
        except (SessionError, TaskError) as exc:
            await message.answer(str(exc))
            return
        for chunk in split_text(reply):
            await message.answer(chunk)

    @router.message(F.text.startswith("/"))
    async def on_unknown_command(message: Message, ctx: AppContext):
        await message.answer("Unknown command. Use /help for the list of commands.")

    return router


def _start_text() -> str:
    return dashboard_text()


def _parse_task_id(message: Message) -> int | None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1].strip())
    except ValueError:
        return None
