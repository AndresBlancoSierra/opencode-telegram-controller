"""Tests for database persistence across restarts."""

from __future__ import annotations

import aiosqlite

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


async def test_sessions_survive_reconnect(tmp_path):
    db = Database(tmp_path / "tasks.db")
    await db.connect()
    repo = TaskRepository(db)
    session = await repo.create_session(user_id=1, project_id="A", opencode_session_id="ses_x")
    await repo.set_active_session(1, session.id)
    await repo.touch_session(session.id, opencode_session_id="ses_y")
    await db.close()

    db2 = Database(tmp_path / "tasks.db")
    await db2.connect()
    repo2 = TaskRepository(db2)
    active = await repo2.get_active_session(1)
    assert active is not None
    assert active.id == session.id
    assert active.opencode_session_id == "ses_y"
    assert active.project_id == "A"
    listed = await repo2.list_sessions(1)
    assert [s.id for s in listed] == [session.id]
    await db2.close()


async def test_sessions_are_per_user(db):
    repo = TaskRepository(db)
    a = await repo.create_session(user_id=1, project_id="A")
    await repo.set_active_session(1, a.id)
    assert await repo.get_active_session(2) is None
    assert await repo.get_session_by_opencode_id(2, "ses_x") is None
    await db.close()


_LEGACY_SCHEMA = """
CREATE TABLE tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    project_id    TEXT    NOT NULL,
    prompt        TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    exit_code     INTEGER,
    session_id    TEXT,
    model         TEXT,
    error         TEXT,
    summary       TEXT,
    log_tail      TEXT,
    commit_created TEXT
);
CREATE TABLE user_state (
    user_id         INTEGER PRIMARY KEY,
    active_project  TEXT
);
"""


async def test_migration_adds_session_columns_and_keeps_legacy_tasks(tmp_path):
    db_path = tmp_path / "migrate.db"
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_LEGACY_SCHEMA)
    await conn.execute(
        "INSERT INTO tasks (user_id, project_id, prompt, status, created_at) "
        "VALUES (1, 'A', 'legacy prompt', 'COMPLETED', '2026-01-01T00:00:00+00:00')"
    )
    await conn.commit()
    await conn.close()

    db = Database(db_path)
    await db.connect()
    repo = TaskRepository(db)
    stored = await repo.get_task(1)
    assert stored is not None
    assert stored.prompt == "legacy prompt"
    assert stored.session_internal_id is None
    assert stored.interactive is False
    session = await repo.create_session(user_id=1, project_id="A")
    assert session.id is not None
    await db.close()


async def test_recover_interrupted_keeps_sessions_intact(db):
    repo = TaskRepository(db)
    session = await repo.create_session(user_id=1, project_id="A", opencode_session_id="ses_1")
    await repo.set_active_session(1, session.id)
    running = await repo.create_task(
        user_id=1,
        project_id="A",
        prompt="running task",
        session_id="ses_1",
        session_internal_id=session.id,
    )
    await repo.mark_started(running.id)
    interrupted = await repo.recover_interrupted()
    assert interrupted == [running.id]
    active = await repo.get_active_session(1)
    assert active is not None
    assert active.opencode_session_id == "ses_1"
