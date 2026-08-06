"""Helpers for building and splitting Telegram messages."""

from __future__ import annotations

from .models import Task, TaskStatus

MAX_MESSAGE_CHARS = 4000


def split_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split text into chunks that fit a Telegram message.

    Chunks are split on newlines when possible so lines are not torn apart.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def status_emoji(status: TaskStatus) -> str:
    return {
        TaskStatus.PENDING: "⏳",
        TaskStatus.RUNNING: "🔄",
        TaskStatus.COMPLETED: "✅",
        TaskStatus.FAILED: "❌",
        TaskStatus.CANCELLED: "🛑",
    }[status]


def format_task_line(task: Task) -> str:
    emoji = status_emoji(task.status)
    prompt = task.prompt.replace("\n", " ")
    if len(prompt) > 60:
        prompt = prompt[:57] + "..."
    return f"{emoji} #{task.id}  {prompt}"


def format_task_detail(task: Task) -> str:
    lines = [
        f"{status_emoji(task.status)} Task #{task.id}",
        f"Project: {task.project_id}",
        f"Status: {task.status.value}",
        "",
        f"Prompt: {task.prompt}",
    ]
    if task.started_at:
        lines.append(f"Started: {task.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if task.finished_at:
        lines.append(f"Finished: {task.finished_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if task.duration_seconds is not None:
        lines.append(f"Duration: {format_duration(task.duration_seconds)}")
    if task.exit_code is not None:
        lines.append(f"Exit code: {task.exit_code}")
    if task.session_id:
        lines.append(f"OpenCode session: {task.session_id}")
    if task.error:
        lines.append(f"Error: {task.error}")
    return "\n".join(lines)


def format_projects_list(projects, active: str | None) -> str:
    lines = ["📁 Available projects:"]
    for project in projects:
        marker = "• " if project.enabled else "✖ "
        line = f"{marker}{project.name}"
        if project.description:
            line += f" — {project.description}"
        if active == project.name:
            line += "  👈 active"
        if not project.enabled:
            line += "  (disabled)"
        lines.append(line)
    lines.append("")
    lines.append("Use /use <name> to switch the active project.")
    return "\n".join(lines)


def help_text() -> str:
    return (
        "🤖 OpenCode Telegram Controller\n\n"
        "Control OpenCode on your PC from Telegram.\n\n"
        "Commands:\n"
        "/start — welcome message\n"
        "/help — this help\n"
        "/status — active project, running tasks, queue\n"
        "/tasks — recent tasks\n"
        "/task <id> — task details\n"
        "/cancel [id] — cancel a running or queued task\n"
        "/logs [id] — recent logs of a task\n"
        "/projects — list available projects\n"
        "/use <name> — select the active project\n\n"
        "Natural language:\n"
        "Send any plain text message to create a task in the active project. "
        "For example:\n"
        "  Fix the failing tests in this project and create a commit."
    )
