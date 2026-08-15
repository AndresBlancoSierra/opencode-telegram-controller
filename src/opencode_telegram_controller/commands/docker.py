"""Docker commands: /docker /docker_status /docker_restart /docker_logs.

Handlers are thin and registered onto the bot's router by :func:`register`.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services.docker import DockerManager
from .common import audit, check_permission, format_service_error


async def on_docker(message: Message, ctx) -> None:
    """Show a summary of Docker containers."""
    if not await check_permission(ctx, message, "docker"):
        return
    docker: DockerManager | None = getattr(ctx, "docker", None)
    if docker is None:
        await message.answer("❌ Docker unavailable (daemon or CLI not reachable).")
        return
    try:
        summary = await docker.summary()
    except Exception as exc:
        await message.answer(format_service_error("Docker", exc))
        return
    await message.answer(_render_summary(summary))


async def on_docker_status(message: Message, ctx) -> None:
    """List every Docker container with its status."""
    if not await check_permission(ctx, message, "docker_status"):
        return
    docker: DockerManager | None = getattr(ctx, "docker", None)
    if docker is None:
        await message.answer("❌ Docker unavailable (daemon or CLI not reachable).")
        return
    try:
        containers = await docker.containers()
    except Exception as exc:
        await message.answer(format_service_error("Docker", exc))
        return
    await message.answer(_render_containers(containers))


async def on_docker_restart(message: Message, ctx) -> None:
    """Restart an allowlisted container: /docker_restart <name>."""
    if not await check_permission(ctx, message, "docker_restart"):
        return
    docker: DockerManager | None = getattr(ctx, "docker", None)
    if docker is None:
        await message.answer("❌ Docker unavailable (daemon or CLI not reachable).")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /docker_restart <container>")
        return
    name = " ".join(parts[1:]).strip()
    try:
        await docker.restart(name)
    except Exception as exc:
        await message.answer(format_service_error("Docker", exc))
        return
    await audit(ctx, message, "docker.restart", target=name)
    await message.answer(f"✅ Container <code>{name}</code> restarted.")


async def on_docker_logs(message: Message, ctx) -> None:
    """Show the tail of a container's logs: /docker_logs <name> [lines]."""
    if not await check_permission(ctx, message, "docker_logs"):
        return
    docker: DockerManager | None = getattr(ctx, "docker", None)
    if docker is None:
        await message.answer("❌ Docker unavailable (daemon or CLI not reachable).")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /docker_logs <container> [lines]")
        return
    name = parts[1].strip()
    lines_arg = parts[2].strip() if len(parts) > 2 else ""
    try:
        lines = int(lines_arg) if lines_arg.isdigit() else 200
        logs = await docker.logs(name, lines)
    except Exception as exc:
        await message.answer(format_service_error("Docker", exc))
        return
    if not logs.strip():
        logs = "(no logs)"
    await message.answer(f"📄 Logs of <code>{name}</code>:\n<pre>{logs[-3500:]}</pre>")


def _render_summary(summary) -> str:
    emoji = "🐳" if summary.running else "🐳"
    return (
        f"{emoji} <b>DOCKER</b>\n"
        f"Containers: <b>{summary.total}</b> ({summary.healthy_text})"
        f"{f'\nStopped: <b>{summary.stopped}</b>' if summary.stopped else ''}"
        f"{f'\nUnhealthy: <b>{summary.unhealthy}</b>' if summary.unhealthy else ''}"
    )


def _render_containers(containers) -> str:
    lines = ["🐳 <b>DOCKER STATUS</b>"]
    if not containers:
        lines.append("No containers.")
        return "\n".join(lines)
    for container in containers:
        state = container.state
        if state == "running":
            icon = "🟢"
        elif state == "paused":
            icon = "🟡"
        elif container.health == "unhealthy":
            icon = "🔴"
        else:
            icon = "⚪"
        name = container.name if len(container.name) <= 30 else container.name[:27] + "..."
        lines.append(
            f"{icon} <code>{name}</code> · {container.status}"
            + (f" · <i>{container.health}</i>" if container.health else "")
        )
    return "\n".join(lines)


def register(router: Router) -> None:
    """Register this command group onto an existing router."""
    router.message.register(on_docker, Command("docker"))
    router.message.register(on_docker_status, Command("docker_status"))
    router.message.register(on_docker_restart, Command("docker_restart"))
    router.message.register(on_docker_logs, Command("docker_logs"))
