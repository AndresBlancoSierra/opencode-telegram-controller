"""System and dashboard commands: /status /resources /disk /processes /health.

Handlers are thin and registered onto the bot's router by :func:`register`.
Keeping them on one router avoids aiogram include/propagation ordering issues.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..formatting import (
    format_disk_list,
    format_memory,
    format_processes,
    format_resources,
    format_uptime,
)
from .common import check_permission, format_service_error


async def on_status(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "/status"):
        return
    if ctx.system is None:
        await message.answer("❌ System capability is not available.")
        return
    try:
        snapshot = await ctx.system.snapshot()
    except Exception as exc:
        await message.answer(format_service_error("System", exc))
        return

    running = await ctx.repo.count_running()
    pending = len(await ctx.repo.next_pending())
    project = await ctx.manager.active_project(message.from_user.id)
    session = await ctx.manager.active_session(message.from_user.id)
    session_line = "none"
    if session is not None:
        session_line = session.opencode_session_id or f"#{session.id}"

    lines = [
        "🖥 SYSTEM STATUS",
        "",
        f"CPU      {snapshot.cpu_percent:.0f}%",
        f"RAM      {format_memory(snapshot.memory)}",
        f"DISK     {snapshot.disk_usage_percent}%",
        f"UPTIME   {format_uptime(snapshot.uptime_seconds)}",
        "",
        "🤖 OpenCode",
        f"Active project: {project.name if project else 'none'}",
        f"Active session: {session_line}",
        f"Running tasks: {running}",
        f"Queued tasks: {pending}",
    ]
    await message.answer("\n".join(lines))


async def on_resources(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "/resources"):
        return
    if ctx.system is None:
        await message.answer("❌ System capability is not available.")
        return
    try:
        resources = await ctx.system.resources()
    except Exception as exc:
        await message.answer(format_service_error("Resources", exc))
        return
    await message.answer(format_resources(resources))


async def on_disk(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "/disk"):
        return
    if ctx.system is None:
        await message.answer("❌ System capability is not available.")
        return
    try:
        infos = await ctx.system.disk()
    except Exception as exc:
        await message.answer(format_service_error("Disk", exc))
        return
    await message.answer(format_disk_list(infos))


async def on_processes(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "/processes"):
        return
    if ctx.system is None:
        await message.answer("❌ System capability is not available.")
        return
    try:
        snapshots = await ctx.system.processes()
    except Exception as exc:
        await message.answer(format_service_error("Processes", exc))
        return
    await message.answer(format_processes(snapshots))


async def on_health(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "/health"):
        return
    if ctx.monitoring is None:
        await message.answer("❌ Health monitoring is not available.")
        return
    try:
        checks = await ctx.monitoring.check()
    except Exception as exc:
        await message.answer(format_service_error("Health", exc))
        return
    await message.answer(ctx.monitoring.render(checks))


def register(router: Router) -> None:
    """Register this command group onto an existing router."""
    router.message.register(on_status, Command("status"))
    router.message.register(on_resources, Command("resources"))
    router.message.register(on_disk, Command("disk"))
    router.message.register(on_processes, Command("processes"))
    router.message.register(on_health, Command("health"))
