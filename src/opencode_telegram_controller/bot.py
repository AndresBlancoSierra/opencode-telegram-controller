"""Telegram bot: authorization, commands and natural-language task intake.

Uses aiogram 3 with long polling (no webhook, no exposed ports). Every update
is checked against the user allowlist before any handler runs.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import TelegramAPIServer
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from .auth import AuthorizationService
from .config import Settings
from .formatting import (
    format_projects_list,
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
from .task_executor import TaskExecutor
from .task_manager import TaskError, TaskManager


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


class ContextMiddleware(BaseMiddleware):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self._ctx = ctx

    async def __call__(self, handler, event, data):
        data["ctx"] = self._ctx
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
        await message.answer(help_text())

    @router.message(Command("help"))
    async def on_help(message: Message, ctx: AppContext):
        await message.answer(help_text())

    @router.message(Command("status"))
    async def on_status(message: Message, ctx: AppContext):
        running = await ctx.repo.count_running()
        pending = len(await ctx.repo.next_pending())
        project = await ctx.manager.active_project(message.from_user.id)
        uptime = datetime.now(UTC) - ctx.started_at
        minutes, seconds = divmod(int(uptime.total_seconds()), 60)
        lines = [
            "📊 Status",
            f"Active project: {project.name if project else 'none'}",
            f"Running tasks: {running}",
            f"Queued tasks: {pending}",
            f"Concurrency: {ctx.settings.max_concurrent_tasks}",
            f"Model: {ctx.settings.opencode_model or 'default'}",
            f"Uptime: {minutes}m {seconds}s",
        ]
        await message.answer("\n".join(lines))

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
        tasks = await ctx.repo.list_tasks(limit=10)
        if not tasks:
            await message.answer("No tasks yet. Send a message to create one.")
            return
        lines = ["📋 Recent tasks:"]
        lines.extend(format_task_line(t) for t in tasks)
        lines.append("")
        lines.append("Use /task <id> for details.")
        await message.answer("\n".join(lines))

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
        project = await ctx.manager.active_project(user_id)
        if project is None:
            await message.answer(
                "No active project. Use /projects to list projects and /use <name> to select one."
            )
            return
        try:
            task = await ctx.manager.create_task(user_id, project.name, message.text)
        except TaskError as exc:
            await message.answer(str(exc))
            return
        await message.answer(
            f"🧠 Task #{task.id} created in project {project.name}.\n"
            f"Prompt: {message.text}\n\nStatus: ⏳ queued"
        )

    @router.message(F.text.startswith("/"))
    async def on_unknown_command(message: Message, ctx: AppContext):
        await message.answer("Unknown command. Use /help for the list of commands.")

    return router


def _parse_task_id(message: Message) -> int | None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1].strip())
    except ValueError:
        return None
