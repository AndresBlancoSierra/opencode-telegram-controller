"""Helpers for building and splitting Telegram messages."""

from __future__ import annotations

from .models import Session, Task, TaskStatus

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


def session_display_id(session: Session) -> str:
    return session.opencode_session_id or f"#{session.id}"


def shorten_session_id(session_id: str, limit: int = 12) -> str:
    if len(session_id) <= limit:
        return session_id
    return f"{session_id[:limit]}…"


def format_relative_time(dt, now=None) -> str:
    from datetime import UTC, datetime

    now = now or datetime.now(UTC)
    delta = now - dt
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def format_sessions_list(sessions: list[Session], active_id: int | None = None) -> str:
    if not sessions:
        return "No sessions yet.\n\nSend /new to start one."
    lines = ["🗂️ Your sessions:"]
    for session in sessions:
        marker = "• "
        display = session_display_id(session)
        title = f" {session.title}" if session.title else ""
        age = format_relative_time(session.updated_at)
        extra = "  👈 active" if active_id == session.id else ""
        lines.append(
            f"{marker}#{session.id}  {display}{title} — {session.project_id} — {age}{extra}"
        )
    lines.append("")
    lines.append("Use /continue <id> to resume a session, or /new to start one.")
    return "\n".join(lines)


def format_session_detail(session: Session, task_count: int) -> str:
    display = session_display_id(session)
    lines = [
        f"🧠 Session #{session.id}",
        f"ID: {display}",
        f"Project: {session.project_id}",
        f"Messages: {task_count}",
        f"Created: {session.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Updated: {session.updated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Active: {'yes' if session.is_active else 'no'}",
    ]
    if session.title:
        lines.append(f"Title: {session.title}")
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
        "Sessions:\n"
        "/new — start a new OpenCode session\n"
        "/history — list your past sessions\n"
        "/continue <id> — resume a session (id or ses_...)\n"
        "/current — details of the active session\n\n"
        "Commands:\n"
        "/start — welcome message\n"
        "/help — this help\n"
        "/status — active project, session, running tasks\n"
        "/tasks [all] — messages of the active session (or global history)\n"
        "/task <id> — task details\n"
        "/cancel [id] — cancel a running or queued task\n"
        "/logs [id] — recent logs of a task\n"
        "/projects — list available projects\n"
        "/use <name> — select the active project\n\n"
        "Natural language:\n"
        "Send any plain text message to continue the active session. "
        "For example:\n"
        "  Fix the failing tests in this project and create a commit."
    )
