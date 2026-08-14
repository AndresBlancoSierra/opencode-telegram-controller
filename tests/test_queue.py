"""Tests for queue dispatch and concurrency."""

from __future__ import annotations

import asyncio

from conftest import make_settings

from opencode_telegram_controller.models import TaskStatus
from opencode_telegram_controller.queue_worker import QueueWorker


class FakeExecutor:
    """In-memory executor for queue tests."""

    def __init__(self, repo, delay=0.0):
        self.repo = repo
        self.delay = delay
        self.executed: list[int] = []
        self.fail_on: set[int] = set()
        self.completion_waits: dict[int, asyncio.Future[str]] = {}

    async def execute(self, task_id: int):
        self.executed.append(task_id)
        await self.repo.get_task(task_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if task_id in self.fail_on:
            await self.repo.mark_finished(task_id, TaskStatus.FAILED, error="boom")
        else:
            await self.repo.mark_finished(task_id, TaskStatus.COMPLETED, exit_code=0)

    def register_completion_wait(self, task_id: int) -> asyncio.Future[str]:
        future = asyncio.get_running_loop().create_future()
        self.completion_waits[task_id] = future
        return future

    def resolve_completion(self, task_id: int, text: str) -> None:
        future = self.completion_waits.pop(task_id, None)
        if future is not None and not future.done():
            future.set_result(text)


def make_worker(repo, registry, max_concurrent=1, executor=None, notifier=None):
    settings = make_settings(max_concurrent_tasks=max_concurrent)
    executor = executor or FakeExecutor(repo)
    return (
        QueueWorker(
            repo=repo,
            registry=registry,
            executor=executor,
            notifier=notifier,
            settings=settings,
        ),
        executor,
    )


async def test_single_dispatch(repo, registry):
    worker, executor = make_worker(repo, registry)
    task = await repo.create_task(user_id=1, project_id="A", prompt="one")
    dispatched = await worker.dispatch()
    assert dispatched == 1
    await asyncio.sleep(0.05)
    assert executor.executed == [task.id]
    assert (await repo.get_task(task.id)).status == TaskStatus.COMPLETED


async def test_single_concurrent_serializes(repo, registry):
    worker, executor = make_worker(repo, registry, max_concurrent=1)
    a1 = await repo.create_task(user_id=1, project_id="A", prompt="first")
    a2 = await repo.create_task(user_id=1, project_id="A", prompt="second")
    dispatched = await worker.dispatch()
    assert dispatched == 1
    await asyncio.sleep(0.05)
    assert executor.executed == [a1.id]
    assert (await repo.get_task(a2.id)).status == TaskStatus.PENDING
    dispatched = await worker.dispatch()
    assert dispatched == 1
    await asyncio.sleep(0.05)
    assert executor.executed == [a1.id, a2.id]


async def test_project_isolation(repo, registry):
    worker, executor = make_worker(repo, registry, max_concurrent=2)
    a = await repo.create_task(user_id=1, project_id="A", prompt="in A")
    b = await repo.create_task(user_id=1, project_id="B", prompt="in B")
    dispatched = await worker.dispatch()
    assert dispatched == 2
    await asyncio.sleep(0.05)
    assert sorted(executor.executed) == sorted([a.id, b.id])


async def test_same_project_serialized_even_with_capacity(repo, registry):
    worker, executor = make_worker(repo, registry, max_concurrent=2)
    a1 = await repo.create_task(user_id=1, project_id="A", prompt="a1")
    a2 = await repo.create_task(user_id=1, project_id="A", prompt="a2")
    b = await repo.create_task(user_id=1, project_id="B", prompt="b")
    dispatched = await worker.dispatch()
    assert dispatched == 2
    await asyncio.sleep(0.05)
    assert sorted(executor.executed) == sorted([a1.id, b.id])
    assert (await repo.get_task(a2.id)).status == TaskStatus.PENDING


async def test_same_session_serialized_even_with_capacity(repo, registry):
    worker, executor = make_worker(repo, registry, max_concurrent=2)
    session = await repo.create_session(user_id=1, project_id="A")
    s1 = await repo.create_task(
        user_id=1, project_id="A", prompt="s1", session_internal_id=session.id
    )
    s2 = await repo.create_task(
        user_id=1, project_id="A", prompt="s2", session_internal_id=session.id
    )
    b = await repo.create_task(user_id=1, project_id="B", prompt="b")
    dispatched = await worker.dispatch()
    assert dispatched == 2
    await asyncio.sleep(0.05)
    assert sorted(executor.executed) == sorted([s1.id, b.id])
    assert (await repo.get_task(s2.id)).status == TaskStatus.PENDING


async def test_session_messages_executed_in_order(repo, registry):
    worker, executor = make_worker(repo, registry, max_concurrent=1)
    session = await repo.create_session(user_id=1, project_id="A")
    tasks = []
    for prompt in ("first", "second", "third"):
        tasks.append(
            await repo.create_task(
                user_id=1, project_id="A", prompt=prompt, session_internal_id=session.id
            )
        )
    for _task in tasks:
        await worker.dispatch()
        await asyncio.sleep(0.05)
    assert executor.executed == [t.id for t in tasks]
    assert [(await repo.get_task(t.id)).status for t in tasks] == [TaskStatus.COMPLETED] * 3


async def test_running_tasks_not_redispatched(repo, registry):
    worker, executor = make_worker(repo, registry)
    running = await repo.create_task(user_id=1, project_id="A", prompt="already running")
    await repo.mark_started(running.id)
    await repo.create_task(user_id=1, project_id="A", prompt="queued behind")
    dispatched = await worker.dispatch()
    assert dispatched == 0
    assert executor.executed == []


async def test_marked_running_before_execution(repo, registry):
    settings = make_settings(max_concurrent_tasks=1)
    executor = FakeExecutor(repo, delay=0.05)
    worker = QueueWorker(
        repo=repo, registry=registry, executor=executor, notifier=None, settings=settings
    )
    a1 = await repo.create_task(user_id=1, project_id="A", prompt="first")
    dispatch_task = asyncio.create_task(worker.dispatch())
    await asyncio.sleep(0.01)
    assert (await repo.get_task(a1.id)).status == TaskStatus.RUNNING
    await dispatch_task


async def test_missing_project_task_failed(repo, registry, notifier, adapter):
    from conftest import make_executor as build_executor

    settings = make_settings(max_concurrent_tasks=1)
    executor = build_executor(adapter, repo, registry, notifier, settings)
    worker = QueueWorker(
        repo=repo, registry=registry, executor=executor, notifier=notifier, settings=settings
    )
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    await repo.conn.execute("UPDATE tasks SET project_id = 'Gone' WHERE id = ?", (task.id,))
    await repo.conn.commit()
    dispatched = await worker.dispatch()
    assert dispatched == 1
    await asyncio.sleep(0.05)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.FAILED
    assert "project" in (stored.error or "").lower()


async def test_executor_exception_marks_failed(repo, registry):
    settings = make_settings(max_concurrent_tasks=1)
    executor = FakeExecutor(repo)
    executor.fail_on = set()

    async def exploding(task_id):
        raise RuntimeError("kaboom")

    executor.execute = exploding
    worker = QueueWorker(
        repo=repo, registry=registry, executor=executor, notifier=None, settings=settings
    )
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    await worker.dispatch()
    await asyncio.sleep(0.05)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.FAILED
    assert "Internal worker error" in (stored.error or "")
