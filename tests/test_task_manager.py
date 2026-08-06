"""Tests for TaskManager: validation, duplicates, cancellation."""

from __future__ import annotations

import pytest
from conftest import make_executor, make_settings

from opencode_telegram_controller.models import TaskStatus
from opencode_telegram_controller.task_manager import TaskError, TaskManager

USER_ID = 123


@pytest.fixture
def manager(repo, registry, notifier):
    settings = make_settings(default_timeout_seconds=5, progress_interval_seconds=3600)
    executor = make_executor(None, repo, registry, notifier, settings)
    return TaskManager(
        repo=repo, registry=registry, notifier=notifier, executor=executor, settings=settings
    )


async def test_create_task_pending(repo, manager):
    task = await manager.create_task(USER_ID, "A", "fix the bug")
    assert task.status == TaskStatus.PENDING
    assert task.prompt == "fix the bug"
    assert task.project_id == "A"
    assert task.user_id == USER_ID
    stored = await repo.get_task(task.id)
    assert stored is not None


async def test_create_task_with_known_project(repo, manager):
    task = await manager.create_task(USER_ID, "A", "do something")
    assert task.project_id == "A"
    assert task.project_id == manager._registry.default_project


async def test_create_task_unknown_project(repo, manager):
    with pytest.raises(TaskError, match="Unknown or disabled"):
        await manager.create_task(USER_ID, "Nope", "x")


async def test_create_task_disabled_project(repo, manager):
    with pytest.raises(TaskError, match="Unknown or disabled"):
        await manager.create_task(USER_ID, "C", "x")


async def test_create_task_empty_prompt(repo, manager):
    with pytest.raises(TaskError, match="empty"):
        await manager.create_task(USER_ID, "A", "   ")


async def test_create_task_too_long(repo, manager):
    with pytest.raises(TaskError, match="too long"):
        await manager.create_task(USER_ID, "A", "x" * 5001)


async def test_duplicate_task_rejected(repo, manager):
    await manager.create_task(USER_ID, "A", "same task")
    with pytest.raises(TaskError, match="already"):
        await manager.create_task(USER_ID, "A", "same task")


async def test_duplicate_allowed_in_other_project(repo, manager):
    await manager.create_task(USER_ID, "A", "same task")
    task = await manager.create_task(USER_ID, "B", "same task")
    assert task.status == TaskStatus.PENDING


async def test_duplicate_after_completion_allowed(repo, manager):
    task = await manager.create_task(USER_ID, "A", "run me")
    await repo.mark_finished(task.id, TaskStatus.COMPLETED, exit_code=0)
    new_task = await manager.create_task(USER_ID, "A", "run me")
    assert new_task.id != task.id


async def test_cancel_running_requests_cancellation(repo, manager):
    task = await manager.create_task(USER_ID, "A", "long task")
    await repo.mark_started(task.id)
    await manager.cancel_task(USER_ID, task.id)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.RUNNING


async def test_cancel_pending(repo, manager):
    task = await manager.create_task(USER_ID, "A", "queued task")
    await manager.cancel_task(USER_ID, task.id)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.CANCELLED


async def test_cancel_terminal_task_errors(repo, manager):
    task = await manager.create_task(USER_ID, "A", "done task")
    await repo.mark_finished(task.id, TaskStatus.COMPLETED, exit_code=0)
    with pytest.raises(TaskError, match="already"):
        await manager.cancel_task(USER_ID, task.id)


async def test_cancel_missing_task(repo, manager):
    with pytest.raises(TaskError, match="No task with id"):
        await manager.cancel_task(USER_ID, 99999)


async def test_cancel_other_users_task(repo, manager):
    task = await manager.create_task(USER_ID, "A", "mine")
    with pytest.raises(TaskError, match="only cancel your own"):
        await manager.cancel_task(USER_ID + 1, task.id)


async def test_active_project_falls_back_to_default(repo, manager):
    project = await manager.active_project(USER_ID)
    assert project is not None
    assert project.name == "A"


async def test_active_project_respects_user_choice(repo, manager):
    await manager.set_active_project(USER_ID, "B")
    project = await manager.active_project(USER_ID)
    assert project.name == "B"


async def test_notification_on_queue(manager, notifier):
    await manager.create_task(USER_ID, "A", "hi")
    assert notifier.queued and notifier.messages
