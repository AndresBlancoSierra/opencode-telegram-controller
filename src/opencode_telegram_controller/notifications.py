"""Telegram notification manager.

Sends lifecycle notifications (started, progress, completed, failed, cancelled)
to the authorized users. Notification failures are logged and swallowed so a
transient Telegram error never breaks task execution.

Messages are plain text (no parse mode) so user-provided content containing
markdown characters cannot corrupt rendering or raise parsing errors.
"""

from __future__ import annotations

from loguru import logger

from .formatting import split_text
from .models import Project, Task

PROGRESS_SNIPPET_LIMIT = 200


class NotificationManager:
    def __init__(self, bot, chat_ids: list[int]):
        self._bot = bot
        self._chat_ids = list(chat_ids)

    async def send(self, text: str) -> None:
        for chunk in split_text(text):
            for chat_id in self._chat_ids:
                try:
                    await self._bot.send_message(chat_id=chat_id, text=chunk)
                except Exception:
                    logger.exception("Failed to send Telegram notification to {}", chat_id)

    async def notify_task_queued(self, task: Task, project: Project | None) -> None:
        name = project.name if project else task.project_id
        await self.send(
            f"🧠 OpenCode task queued\n\n"
            f"Project: {name}\nTask: #{task.id}\n\n"
            f"Prompt: {task.prompt}\n\n"
            f"Status: ⏳ PENDING"
        )

    async def notify_task_started(self, task: Task, project: Project | None) -> None:
        name = project.name if project else task.project_id
        await self.send(
            f"🧠 OpenCode task started\n\n"
            f"Project: {name}\nTask: #{task.id}\n\n"
            f"Prompt: {task.prompt}\n\n"
            f"Status: 🔄 Running"
        )

    async def notify_progress(self, task: Task, snippet: str) -> None:
        snippet = snippet.strip()
        if len(snippet) > PROGRESS_SNIPPET_LIMIT:
            snippet = snippet[: PROGRESS_SNIPPET_LIMIT - 3] + "..."
        await self.send(
            f"🔄 Task #{task.id} still running\n\n"
            f"Latest output:\n{snippet}\n\n"
            f"Use /logs {task.id} for recent logs."
        )

    async def notify_task_completed(self, task: Task, summary: str | None) -> None:
        header = f"✅ Task completed\n\nProject: {task.project_id}\nTask: #{task.id}"
        if task.duration_seconds is not None:
            header += f"\nDuration: {task.duration_seconds:.0f}s"
        text = header
        if summary:
            text += f"\n\n{summary}"
        else:
            text += "\n\nNo summary was generated."
        await self.send(text)

    async def notify_task_failed(self, task: Task) -> None:
        text = (
            f"❌ Task failed\n\n"
            f"Project: {task.project_id}\nTask: #{task.id}\n"
            f"Error: {task.error or 'unknown error'}"
        )
        if task.session_id:
            text += f"\nOpenCode session: {task.session_id}"
        if task.log_tail:
            tail = task.log_tail[-500:]
            text += f"\n\nRecent output:\n{tail}"
        await self.send(text)

    async def notify_task_cancelled(self, task: Task) -> None:
        await self.send(f"🛑 Task cancelled\n\nProject: {task.project_id}\nTask: #{task.id}")
