"""High-level task operations used by the Telegram handlers."""

from __future__ import annotations

from .config import Settings
from .models import Project, Task, TaskStatus
from .notifications import NotificationManager
from .projects import ProjectRegistry
from .repository import TaskRepository


class TaskError(Exception):
    """A user-facing task operation error."""


class TaskManager:
    def __init__(
        self,
        *,
        repo: TaskRepository,
        registry: ProjectRegistry,
        notifier: NotificationManager,
        executor,
        settings: Settings,
    ):
        self._repo = repo
        self._registry = registry
        self._notifier = notifier
        self._executor = executor
        self._settings = settings

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
            await self._notifier.notify_task_cancelled(await self._repo.get_task(task_id))
            return task
        raise TaskError(f"Task #{task_id} is already {task.status.value.lower()}.")

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
        return project
