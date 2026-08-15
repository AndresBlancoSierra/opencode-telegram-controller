"""Execution of a single OpenCode task.

Responsibilities:
* launch the OpenCode subprocess through the adapter,
* stream and parse events, track the session id and log tail,
* rate-limited progress notifications,
* timeout enforcement and graceful cancellation,
* collect the session export and Git state, generate the summary,
* persist the final result and notify the user.

Cancellation is requested through :meth:`TaskExecutor.request_cancel`, which
sets an asyncio event observed by the execution loop.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from .config import Settings
from .gitinfo import commit_created_between, git_state
from .models import Task, TaskStatus
from .notifications import NotificationManager
from .opencode import OpenCodeAdapter
from .opencode.events import is_text_event
from .projects import ProjectRegistry
from .repository import TaskRepository
from .summaries import SummaryGenerator

_LOG_TAIL_LINES = 200


class TaskExecutor:
    def __init__(
        self,
        *,
        adapter: OpenCodeAdapter,
        repo: TaskRepository,
        registry: ProjectRegistry,
        notifier: NotificationManager,
        summary_generator: SummaryGenerator,
        settings: Settings,
    ):
        self._adapter = adapter
        self._repo = repo
        self._registry = registry
        self._notifier = notifier
        self._summarizer = summary_generator
        self._settings = settings
        self._cancel_events: dict[int, asyncio.Event] = {}
        self._completion_futures: dict[int, asyncio.Future[str]] = {}

    def request_cancel(self, task_id: int) -> None:
        """Ask the running execution of ``task_id`` to stop."""
        self._cancel_events.setdefault(task_id, asyncio.Event()).set()

    def register_completion_wait(self, task_id: int) -> asyncio.Future[str]:
        """Register a future that resolves with the final reply of ``task_id``.

        Used by interactive (in-session) messages: the Telegram handler awaits
        this future so the bot answers with the task outcome instead of a
        queued/started/completed notification spam.
        """
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._completion_futures[task_id] = future
        return future

    def resolve_completion(self, task_id: int, text: str) -> None:
        """Resolve (or cancel with ``text``) the registered completion future."""
        future = self._completion_futures.pop(task_id, None)
        if future is not None and not future.done():
            future.set_result(text)

    async def execute(self, task_id: int) -> None:
        task = await self._repo.get_task(task_id)
        if task is None:
            return
        project = self._registry.get(task.project_id)
        if project is None:
            await self._fail(task, "Project is no longer configured")
            return
        cancel_event = self._cancel_events.setdefault(task_id, asyncio.Event())
        session_before = task.session_id

        git_before = await git_state(project.path)
        if not task.interactive:
            await self._notifier.notify_task_started(task, project)

        try:
            handle = await self._adapter.run(
                prompt=task.prompt, cwd=str(project.path), session_id=task.session_id
            )
        except Exception as exc:
            logger.exception("Failed to start OpenCode for task #{}", task_id)
            await self._fail(task, f"Failed to start OpenCode: {exc}")
            return

        state: dict = {"session_id": None, "log_lines": [], "last_text": None}
        read_task = asyncio.create_task(self._read_events(handle, task, cancel_event, state))
        timed_out = False
        try:
            if cancel_event.is_set():
                await handle.cancel()
            else:
                try:
                    await asyncio.wait_for(
                        handle.wait(), timeout=self._settings.default_timeout_seconds
                    )
                except TimeoutError:
                    timed_out = True
                    logger.warning(
                        "Task #{} timed out after {}s",
                        task_id,
                        self._settings.default_timeout_seconds,
                    )
                    await handle.cancel()
        finally:
            await read_task

        rc = handle.process.returncode
        stderr = await handle.stderr_lines()
        state["log_lines"].extend(stderr)
        log_tail = "\n".join(state["log_lines"][-_LOG_TAIL_LINES:])
        session_id = state["session_id"]
        if session_id and task.session_id != session_id:
            await self._repo.set_session_id(task_id, session_id)
        if session_id and task.session_internal_id:
            session = await self._repo.get_session(task.session_internal_id)
            if session and session.opencode_session_id != session_id:
                await self._repo.touch_session(session.id, opencode_session_id=session_id)

        if cancel_event.is_set():
            self._cancel_events.pop(task_id, None)
            task = await self._repo.get_task(task_id)
            await self._repo.mark_finished(
                task_id,
                TaskStatus.CANCELLED,
                exit_code=rc,
                session_id=session_id,
                log_tail=log_tail,
            )
            if task.interactive:
                self.resolve_completion(task_id, "🛑 Task cancelled")
            else:
                await self._notifier.notify_task_cancelled(await self._repo.get_task(task_id))
            return

        if timed_out:
            await self._fail(
                task,
                f"Timed out after {self._settings.default_timeout_seconds}s",
                exit_code=rc,
                session_id=session_id,
                log_tail=log_tail,
            )
            return

        if rc != 0:
            detail = stderr[-3:] if stderr else []
            error = f"OpenCode exited with code {rc}"
            if detail:
                error += f": {' | '.join(detail)[:200]}"
            await self._fail(task, error, exit_code=rc, session_id=session_id, log_tail=log_tail)
            return

        await self._finish_success(
            task, session_id, log_tail, git_before, session_before, last_text=state["last_text"]
        )

    # --- internals -------------------------------------------------------

    async def _read_events(self, handle, task: Task, cancel_event, state: dict) -> None:
        last_progress = time.monotonic()
        async for event in handle.events():
            if cancel_event.is_set():
                break
            if event.session_id:
                state["session_id"] = event.session_id
            if event.text:
                state["log_lines"].append(event.text)
            if is_text_event(event) and event.text:
                state["last_text"] = event.text
                now = time.monotonic()
                if now - last_progress >= self._settings.progress_interval_seconds:
                    last_progress = now
                    await self._notifier.notify_progress(task, event.text)

    async def _finish_success(
        self,
        task: Task,
        session_id: str | None,
        log_tail: str,
        git_before,
        session_before: str | None,
        last_text: str | None = None,
    ) -> None:
        self._cancel_events.pop(task.id, None)
        project = self._registry.get(task.project_id)
        export = await self._adapter.export(session_id) if session_id else {}
        git_after = await git_state(project.path) if project else git_before
        commit_created = await commit_created_between(git_before, git_after)
        try:
            summary = await self._summarizer.generate(
                task=task,
                export=export,
                git_before=git_before,
                git_after=git_after,
                log_tail=log_tail,
            )
        except Exception:
            logger.exception("Summary generation failed for task #{}", task.id)
            summary = None
        task = await self._repo.get_task(task.id)
        await self._repo.mark_finished(
            task.id,
            TaskStatus.COMPLETED,
            exit_code=0,
            session_id=session_id,
            summary=summary,
            log_tail=log_tail,
            commit_created=commit_created,
        )
        if task.interactive:
            reply = summary or "Task finished. No additional details were captured."
            if last_text and last_text.strip():
                reply = last_text
            elif not summary:
                reply = "Task finished. No additional details were captured."
            if session_id and session_id != session_before:
                reply = f"🔑 New OpenCode session: {session_id}\n\n{reply}"
            self.resolve_completion(task.id, reply)
        else:
            await self._notifier.notify_task_completed(await self._repo.get_task(task.id), summary)

    async def _fail(
        self,
        task: Task,
        error: str,
        *,
        exit_code: int | None = None,
        session_id: str | None = None,
        log_tail: str | None = None,
    ) -> None:
        self._cancel_events.pop(task.id, None)
        logger.error("Task #{} failed: {}", task.id, error)
        await self._repo.mark_finished(
            task.id,
            TaskStatus.FAILED,
            exit_code=exit_code,
            session_id=session_id,
            error=error,
            log_tail=log_tail,
        )
        if task.interactive:
            self.resolve_completion(task.id, f"❌ Task failed\n\n{error}")
        else:
            await self._notifier.notify_task_failed(await self._repo.get_task(task.id))
