"""Tests for database persistence across restarts."""

from __future__ import annotations

from opencode_telegram_controller.database import Database
from opencode_telegram_controller.models import TaskStatus
from opencode_telegram_controller.repository import TaskRepository


async def test_tasks_survive_reconnect(tmp_path):
    db_path = tmp_path / "tasks.db"
    db = Database(db_path)
    await db.connect()
    repo = TaskRepository(db)
    task = await repo.create_task(user_id=1, project_id="A", prompt="persist me")
    await repo.mark_started(task.id)
    await db.close()

    db2 = Database(db_path)
    await db2.connect()
    repo2 = TaskRepository(db2)
    stored = await repo2.get_task(task.id)
    assert stored is not None
    assert stored.status == TaskStatus.RUNNING
    assert stored.prompt == "persist me"
    await db2.close()


async def test_recover_interrupted_marks_running_failed(db):
    repo = TaskRepository(db)
    running = await repo.create_task(user_id=1, project_id="A", prompt="running task")
    await repo.mark_started(running.id)
    pending = await repo.create_task(user_id=1, project_id="A", prompt="pending task")
    interrupted = await repo.recover_interrupted()
    assert interrupted == [running.id]
    assert (await repo.get_task(running.id)).status == TaskStatus.FAILED
    assert (await repo.get_task(pending.id)).status == TaskStatus.PENDING


async def test_recover_interrupted_nothing_running(db):
    repo = TaskRepository(db)
    assert await repo.recover_interrupted() == []


async def test_list_tasks_ordering(db):
    repo = TaskRepository(db)
    t1 = await repo.create_task(user_id=1, project_id="A", prompt="first")
    t2 = await repo.create_task(user_id=1, project_id="A", prompt="second")
    listed = await repo.list_tasks(limit=10)
    assert [t.id for t in listed] == [t2.id, t1.id]
    assert listed[0].prompt == "second"


async def test_count_running_and_project_busy(db):
    repo = TaskRepository(db)
    t = await repo.create_task(user_id=1, project_id="A", prompt="x")
    assert await repo.count_running() == 0
    assert not await repo.is_project_busy("A")
    await repo.mark_started(t.id)
    assert await repo.count_running() == 1
    assert await repo.is_project_busy("A")
    assert not await repo.is_project_busy("B")


async def test_user_state_persistence(db):
    repo = TaskRepository(db)
    assert await repo.get_active_project(1) is None
    await repo.set_active_project(1, "B")
    assert await repo.get_active_project(1) == "B"
    await repo.set_active_project(1, "A")
    assert await repo.get_active_project(1) == "A"
