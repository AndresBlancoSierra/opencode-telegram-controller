"""Task and user-state persistence.

All queries go through a single aiosqlite connection, which serializes access
internally, so no additional locking is required for single-process use.
"""

from __future__ import annotations

from datetime import datetime

from .database import Database
from .models import Task, TaskStatus, utcnow


def _row_to_task(row) -> Task:
    return Task(
        id=row["id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        prompt=row["prompt"],
        status=TaskStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        exit_code=row["exit_code"],
        session_id=row["session_id"],
        model=row["model"],
        error=row["error"],
        summary=row["summary"],
        log_tail=row["log_tail"],
        commit_created=row["commit_created"],
    )


class TaskRepository:
    def __init__(self, db: Database):
        self._db = db

    @property
    def conn(self):
        return self._db.conn

    # --- tasks -----------------------------------------------------------

    async def create_task(self, *, user_id: int, project_id: str, prompt: str) -> Task:
        now = utcnow().isoformat()
        cur = await self.conn.execute(
            "INSERT INTO tasks (user_id, project_id, prompt, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, project_id, prompt, TaskStatus.PENDING.value, now),
        )
        await self.conn.commit()
        return await self.get_task(cur.lastrowid)

    async def get_task(self, task_id: int) -> Task | None:
        cur = await self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def list_tasks(self, limit: int = 20, status: TaskStatus | None = None) -> list[Task]:
        if status is not None:
            cur = await self.conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status.value, limit),
            )
        else:
            cur = await self.conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def list_tasks_by_project(self, project_id: str, limit: int = 20) -> list[Task]:
        cur = await self.conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def find_duplicate(self, project_id: str, prompt: str) -> Task | None:
        cur = await self.conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND prompt = ? "
            "AND status IN ('PENDING', 'RUNNING') ORDER BY id DESC LIMIT 1",
            (project_id, prompt),
        )
        row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def mark_started(self, task_id: int) -> None:
        await self.conn.execute(
            "UPDATE tasks SET status = ?, started_at = ? WHERE id = ?",
            (TaskStatus.RUNNING.value, utcnow().isoformat(), task_id),
        )
        await self.conn.commit()

    async def mark_finished(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        exit_code: int | None = None,
        session_id: str | None = None,
        error: str | None = None,
        summary: str | None = None,
        log_tail: str | None = None,
        commit_created: str | None = None,
    ) -> None:
        await self.conn.execute(
            "UPDATE tasks SET status = ?, finished_at = ?, exit_code = ?, "
            "session_id = ?, error = ?, summary = ?, log_tail = ?, "
            "commit_created = ? WHERE id = ?",
            (
                status.value,
                utcnow().isoformat(),
                exit_code,
                session_id,
                error,
                summary,
                log_tail,
                commit_created,
                task_id,
            ),
        )
        await self.conn.commit()

    async def set_session_id(self, task_id: int, session_id: str) -> None:
        await self.conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, task_id)
        )
        await self.conn.commit()

    async def count_running(self) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE status = ?",
            (TaskStatus.RUNNING.value,),
        )
        row = await cur.fetchone()
        return row["c"]

    async def is_project_busy(self, project_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE project_id = ? AND status = ?",
            (project_id, TaskStatus.RUNNING.value),
        )
        row = await cur.fetchone()
        return row["c"] > 0

    async def next_pending(self) -> list[Task]:
        cur = await self.conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY id ASC LIMIT 50",
            (TaskStatus.PENDING.value,),
        )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def recover_interrupted(self) -> list[int]:
        """Mark RUNNING tasks as FAILED after a service restart.

        Returns the ids of the tasks that were interrupted.
        """
        cur = await self.conn.execute(
            "SELECT id FROM tasks WHERE status = ?", (TaskStatus.RUNNING.value,)
        )
        rows = await cur.fetchall()
        ids = [r["id"] for r in rows]
        for task_id in ids:
            await self.mark_finished(
                task_id,
                TaskStatus.FAILED,
                error="Interrupted by service restart",
            )
        return ids

    # --- user state ------------------------------------------------------

    async def get_active_project(self, user_id: int) -> str | None:
        cur = await self.conn.execute(
            "SELECT active_project FROM user_state WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row["active_project"] if row else None

    async def set_active_project(self, user_id: int, project_id: str) -> None:
        await self.conn.execute(
            "INSERT INTO user_state (user_id, active_project) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET active_project = excluded.active_project",
            (user_id, project_id),
        )
        await self.conn.commit()
