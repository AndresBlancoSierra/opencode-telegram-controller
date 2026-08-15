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
        "🤖 <b>OpenCode Telegram Controller</b>\n"
        "Control OpenCode and your PC from Telegram.\n\n"
        "🗂 OpenCode sessions:\n"
        "/new — start a new OpenCode session\n"
        "/history — list past sessions\n"
        "/continue &lt;id&gt; — resume a session\n"
        "/current — details of the active session\n"
        "/tasks [all] — messages of the active session\n"
        "/task &lt;id&gt; — task details\n"
        "/cancel [id] — cancel a running or queued task\n"
        "/logs [id] — recent logs of a task\n"
        "/projects · /use &lt;name&gt; — pick the active project\n\n"
        "🖥 System:\n"
        "/status — live dashboard\n"
        "/resources — CPU, RAM, swap, load\n"
        "/disk — filesystem usage\n"
        "/processes — top CPU/RAM processes\n"
        "/health — system, network, VPN, Docker\n\n"
        "🌐 Network:\n"
        "/ip — public &amp; local IP\n"
        "/dns — resolver and servers\n"
        "/network — interfaces and gateway\n\n"
        "🔐 VPN:\n"
        "/vpn — status or <code>/vpn &lt;country&gt;</code>\n"
        "/vpn_status — details + public IP\n"
        "/vpn_dedicated — dedicated server\n"
        "/vpn_change (alias /cambiar) — reconnect the VPN\n\n"
        "🐳 Docker:\n"
        "/docker · /docker_status\n"
        "/docker_restart &lt;name&gt; · /docker_logs &lt;name&gt; [lines]\n\n"
        "🖼 Desktop:\n"
        "/screenshot · /windows · /lock\n\n"
        "🎥 Media:\n"
        "/photo — capture the camera\n"
        "/record_mic [seconds] — record the microphone\n"
        "/stream · /stream_stop — live screen clips\n"
        "Send an audio/video file to play it on the PC\n\n"
        "⚡ Power (requires confirmation):\n"
        "/reboot · /shutdown · /sleep\n"
        "then /confirm_&lt;action&gt; or cancel with /dismiss\n\n"
        "Natural language:\n"
        "Send any plain text message to continue the active session "
        '(e.g. "Fix the failing tests and commit").'
    )


def dashboard_text() -> str:
    return (
        "🖥 <b>PC Control Bot</b>\n"
        "Controlando el equipo y OpenCode desde Telegram.\n\n"
        "🗂 Tareas OpenCode\n"
        "/new — nueva sesión · /continue &lt;id&gt; — reanudar · /cancel — cancelar\n\n"
        "🌐 Red\n"
        "/ip — IP pública/local · /dns · /network\n"
        "/vpn [país] · /vpn_status\n\n"
        "🐳 Docker\n"
        "/docker · /docker_status · /docker_restart &lt;name&gt; · /docker_logs &lt;name&gt;\n\n"
        "🖼 Escritorio\n"
        "/screenshot · /windows · /lock\n\n"
        "🎥 Media\n"
        "/photo · /record_mic [s] · /stream · /stream_stop\n"
        "Envía audio/video para reproducirlo en el equipo\n\n"
        "⚡ Energía (requieren confirmación)\n"
        "/reboot · /shutdown · /sleep\n\n"
        "/status — dashboard en vivo · /help — lista completa"
    )


# --- PC Control formatting ---------------------------------------------


def format_memory(memory) -> str:
    total_gb = memory.total_mb / 1024
    used_gb = memory.used_mb / 1024
    return f"{used_gb:.1f} / {total_gb:.1f} GB"


def format_uptime(seconds: int) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_swap(swap_used_mb: int, swap_total_mb: int) -> str:
    if not swap_total_mb:
        return "none"
    return f"{swap_used_mb / 1024:.1f} / {swap_total_mb / 1024:.1f} GB"


def format_resources(resources) -> str:
    lines = [
        "🖥 RESOURCES",
        f"Host: {resources.hostname}",
        f"CPU: {resources.cpu.label}",
        f"RAM: {format_memory(resources.memory)}",
        f"Swap: {format_swap(resources.swap_used_mb, resources.swap_total_mb)}",
        f"Load: {resources.load_average[0]:.2f} {resources.load_average[1]:.2f} "
        f"{resources.load_average[2]:.2f}",
        f"Uptime: {format_uptime(resources.uptime_seconds)}",
    ]
    return "\n".join(lines)


def format_disk_list(infos) -> str:
    lines = ["💾 FILESYSTEM"]
    if not infos:
        lines.append("No filesystems detected.")
        return "\n".join(lines)
    for info in infos:
        lines.append(
            f"{info.mount:<16} {info.used_gb:>8.1f} / {info.total_gb:<8.1f} GB {info.percent:>3}%"
        )
    return "\n".join(lines)


def format_processes(snapshot) -> str:
    lines = ["🧮 TOP PROCESSES"]

    lines.append("\nCPU")
    for sample in snapshot.by_cpu:
        lines.append(f"{sample.name:<20} {sample.cpu_percent:>5.1f}%")

    lines.append("\nMEM")
    for sample in snapshot.by_memory:
        lines.append(f"{sample.name:<20} {sample.memory_mb / 1024:>6.2f} GB")
    return "\n".join(lines)
