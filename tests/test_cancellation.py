"""Executor-level cancellation tests."""

from __future__ import annotations

import asyncio

from conftest import event, make_executor, make_settings

from opencode_telegram_controller.models import TaskStatus


def build(adapter, repo, registry, notifier, settings=None):
    settings = settings or make_settings(default_timeout_seconds=30)
    return make_executor(adapter, repo, registry, notifier, settings)


async def test_cancel_before_start(repo, registry, notifier, adapter):
    handle = adapter.queue_run(
        events=[event("step_start"), event("text", text="hello")], exit_code=0
    )
    executor = build(adapter, repo, registry, notifier)
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    executor.request_cancel(task.id)
    await executor.execute(task.id)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.CANCELLED
    assert handle.cancelled is True
    assert notifier.cancelled == [task.id]


async def test_cancel_mid_run(repo, registry, notifier, adapter):
    adapter.queue_run(
        events=[event("step_start"), event("text", text="working...")],
        exit_code=0,
        wait_delay=0.5,
    )
    executor = build(adapter, repo, registry, notifier)
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    await repo.mark_started(task.id)
    run = asyncio.create_task(executor.execute(task.id))
    await asyncio.sleep(0.05)
    executor.request_cancel(task.id)
    await run
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.CANCELLED
    assert notifier.cancelled == [task.id]


async def test_cancel_after_completion_is_noop(repo, registry, notifier, adapter):
    adapter.queue_run(
        events=[event("step_start", session_id="ses_9"), event("step_finish")], exit_code=0
    )
    adapter.exports["ses_9"] = {"info": {}, "messages": []}
    executor = build(adapter, repo, registry, notifier)
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    await executor.execute(task.id)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.COMPLETED
    executor.request_cancel(task.id)
    assert (await repo.get_task(task.id)).status == TaskStatus.COMPLETED
