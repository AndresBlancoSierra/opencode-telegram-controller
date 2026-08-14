"""High-level task and session operations used by the Telegram handlers."""

from __future__ import annotations

import asyncio

from .config import Settings
from .models import Project, Session, Task, TaskStatus
from .notifications import NotificationManager
from .opencode import OpenCodeAdapter
from .projects import ProjectRegistry
from .repository import TaskRepository


class TaskError(Exception):
    """A user-facing task operation error."""


class SessionError(Exception):
    """A user-facing session operation error."""


class TaskManager:
    def __init__(
        self,
        *,
        repo: TaskRepository,
        registry: ProjectRegistry,
        notifier: NotificationManager,
        executor,
        settings: Settings,
        adapter: OpenCodeAdapter | None = None,
    ):
        self._repo = repo
        self._registry = registry
        self._notifier = notifier
        self._executor = executor
        self._settings = settings
        self._adapter = adapter

    async def create_task(self, user_id: int, project_id: str, prompt: str) -> Task:
        try:
            project = self._registry.resolve(project_id)
        except KeyError as exc:
            raise TaskError(f"Unknown or disabled project: {project_id}") from exc
        prompt = prompt.strip()
        if not prompt:
            raise TaskError("The task prompt cannot be empty.")
        if len(prompt) > self._settings.prompt_max_length:
            raise TaskError(f"Prompt too long (max {self._settings.prompt_max_length} characters).")
        duplicate = await self._repo.find_duplicate(project.name, prompt)
        if duplicate is not None:
            state = "running" if duplicate.status == TaskStatus.RUNNING else "queued"
            raise TaskError(f"A task with the same prompt is already {state} (#{duplicate.id}).")
        task = await self._repo.create_task(user_id=user_id, project_id=project.name, prompt=prompt)
        await self._notifier.notify_task_queued(task, project)
        return task

    async def cancel_task(self, user_id: int, task_id: int) -> Task:
        task = await self._repo.get_task(task_id)
        if task is None:
            raise TaskError(f"No task with id {task_id}.")
        if task.user_id != user_id:
            raise TaskError("You can only cancel your own tasks.")
        if task.status == TaskStatus.RUNNING:
            self._executor.request_cancel(task_id)
            return task
        if task.status == TaskStatus.PENDING:
            await self._repo.mark_finished(task_id, TaskStatus.CANCELLED)
            if task.interactive:
                self._executor.resolve_completion(task_id, "🛑 Task cancelled")
            else:
                await self._notifier.notify_task_cancelled(await self._repo.get_task(task_id))
            return task
        raise TaskError(f"Task #{task_id} is already {task.status.value.lower()}.")

    async def send_message(
        self, user_id: int, session: Session, prompt: str, timeout: float | None = None
    ) -> str:
        """Send an interactive message to ``session`` and wait for its reply."""
        prompt = prompt.strip()
        if not prompt:
            raise TaskError("The message cannot be empty.")
        if len(prompt) > self._settings.prompt_max_length:
            raise TaskError(
                f"Message too long (max {self._settings.prompt_max_length} characters)."
            )
        try:
            self._registry.resolve(session.project_id)
        except KeyError as exc:
            raise SessionError(
                f"Session {self._display_id(session)} belongs to a project that is "
                "no longer enabled."
            ) from exc
        if (
            session.opencode_session_id
            and self._adapter is not None
            and not await self._adapter.session_exists(session.opencode_session_id)
        ):
            raise SessionError(
                "The OpenCode session no longer exists on the server. Start a new one with /new."
            )
        task = await self._repo.create_task(
            user_id=user_id,
            project_id=session.project_id,
            prompt=prompt,
            session_id=session.opencode_session_id,
            session_internal_id=session.id,
            interactive=True,
        )
        future = self._executor.register_completion_wait(task.id)
        wait = timeout if timeout is not None else self._settings.default_timeout_seconds * 2 + 300
        try:
            return await asyncio.wait_for(future, timeout=wait)
        except TimeoutError:
            self._executor.request_cancel(task.id)
            return (
                "The task is still running and reached the wait timeout. Use /status to check it."
            )

    # --- sessions --------------------------------------------------------

    async def new_session(self, user_id: int, *, title: str | None = None) -> Session:
        """Create a new session in the active project and make it active."""
        project = await self.active_project(user_id)
        if project is None:
            raise SessionError(
                "No active project. Use /projects and /use <name> to select one first."
            )
        session = await self._repo.create_session(
            user_id=user_id, project_id=project.name, title=title
        )
        await self._repo.set_active_session(user_id, session.id)
        return session

    async def active_session(self, user_id: int) -> Session | None:
        """Return the user's active session, validated against the allowlist."""
        session = await self._repo.get_active_session(user_id)
        if session is None:
            return None
        try:
            self._registry.resolve(session.project_id)
        except KeyError:
            await self._repo.clear_active_session(user_id)
            return None
        return session

    async def continue_session(self, user_id: int, ref: str) -> Session:
        """Restore a session owned by ``user_id`` and make it active.

        ``ref`` is either the internal session id (integer) or the OpenCode
        session id (``ses_...``). Ownership is always enforced against the
        sessions table, never by trusting an arbitrary OpenCode id.
        """
        session = await self._find_session(user_id, ref)
        if session is None:
            raise SessionError(f"No session matching {ref!r} was found for this user.")
        try:
            self._registry.resolve(session.project_id)
        except KeyError as exc:
            raise SessionError(
                f"Session {self._display_id(session)} belongs to a project that is "
                "no longer enabled."
            ) from exc
        if (
            session.opencode_session_id
            and self._adapter is not None
            and not await self._adapter.session_exists(session.opencode_session_id)
        ):
            raise SessionError(
                "The OpenCode session no longer exists on the server. Start a new one with /new."
            )
        await self._repo.set_active_session(user_id, session.id)
        await self._repo.set_active_project(user_id, session.project_id)
        return session

    async def list_sessions(self, user_id: int, limit: int = 25) -> list[Session]:
        return await self._repo.list_sessions(user_id, limit=limit)

    async def session_task_count(self, session_internal_id: int) -> int:
        return await self._repo.count_tasks_in_session(session_internal_id)

    # --- projects --------------------------------------------------------

    async def active_project(self, user_id: int) -> Project | None:
        name = await self._repo.get_active_project(user_id)
        if name:
            project = self._registry.get(name)
            if project and project.enabled:
                return project
        if self._registry.default_project:
            project = self._registry.get(self._registry.default_project)
            if project and project.enabled:
                return project
        return None

    async def set_active_project(self, user_id: int, project_id: str) -> Project:
        project = self._registry.resolve(project_id)
        await self._repo.set_active_project(user_id, project.name)
        await self._repo.clear_active_session(user_id)
        return project

    # --- helpers ---------------------------------------------------------

    async def _find_session(self, user_id: int, ref: str) -> Session | None:
        ref = str(ref).strip()
        if ref.isdigit():
            session = await self._repo.get_session(int(ref))
            if session is not None and session.user_id == user_id:
                return session
            return None
        return await self._repo.get_session_by_opencode_id(user_id, ref)

    @staticmethod
    def _display_id(session: Session) -> str:
        return session.opencode_session_id or f"#{session.id}"
