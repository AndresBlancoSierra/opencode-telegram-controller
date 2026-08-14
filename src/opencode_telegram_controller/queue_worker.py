"""Task queue and dispatch loop.

A single dispatcher loop schedules PENDING tasks. Constraints:

* at most ``max_concurrent_tasks`` RUNNING tasks globally (default 1),
* never two RUNNING tasks in the same project (workspace isolation),
* never two RUNNING tasks in the same session (in-order conversation),
* oldest PENDING tasks first.

Tasks are marked RUNNING synchronously during dispatch, which prevents the
same task from being scheduled twice by concurrent dispatches.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from loguru import logger

from .config import Settings
from .models import TaskStatus
from .notifications import NotificationManager
from .projects import ProjectRegistry
from .repository import TaskRepository
from .task_executor import TaskExecutor

_DISPATCH_INTERVAL_SECONDS = 1.0


class QueueWorker:
    def __init__(
        self,
        *,
        repo: TaskRepository,
        registry: ProjectRegistry,
        executor: TaskExecutor,
        notifier: NotificationManager,
        settings: Settings,
    ):
        self._repo = repo
        self._registry = registry
        self._executor = executor
        self._notifier = notifier
        self._settings = settings
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            await self._loop_task

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.dispatch()
            except Exception:
                logger.exception("Task dispatch error")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=_DISPATCH_INTERVAL_SECONDS)

    async def dispatch(self) -> int:
        """Schedule as many PENDING tasks as the constraints allow.

        Returns the number of tasks dispatched.
        """
        running = await self._repo.count_running()
        pending = await self._repo.next_pending()
        dispatched = 0
        for task in pending:
            if running >= self._settings.max_concurrent_tasks:
                break
            if await self._repo.is_project_busy(task.project_id):
                continue
            if task.session_internal_id is not None and await self._repo.is_session_busy(
                task.session_internal_id
            ):
                continue
            running += 1
            await self._repo.mark_started(task.id)
            logger.info("Dispatching task #{} in project {}", task.id, task.project_id)
            asyncio.create_task(self._run_task(task.id))
            dispatched += 1
        return dispatched

    async def _run_task(self, task_id: int) -> None:
        try:
            await self._executor.execute(task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Task #{} crashed in worker", task_id)
            task = await self._repo.get_task(task_id)
            await self._repo.mark_finished(
                task_id, TaskStatus.FAILED, error="Internal worker error"
            )
            if task is not None and task.interactive:
                self._executor.resolve_completion(
                    task_id, "❌ Task failed\n\nInternal worker error"
                )
