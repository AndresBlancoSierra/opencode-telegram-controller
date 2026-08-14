"""Task and user-state persistence.

All queries go through a single aiosqlite connection, which serializes access
internally, so no additional locking is required for single-process use.
"""

from __future__ import annotations

from datetime import datetime

from .database import Database
from .models import Session, Task, TaskStatus, utcnow


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
        session_internal_id=row["session_internal_id"],
        interactive=bool(row["interactive"]),
    )


def _row_to_session(row) -> Session:
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        opencode_session_id=row["opencode_session_id"],
        title=row["title"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        is_active=bool(row["is_active"]),
    )


class TaskRepository:
    def __init__(self, db: Database):
        self._db = db

    @property
    def conn(self):
        return self._db.conn

    # --- tasks -----------------------------------------------------------

    async def create_task(
        self,
        *,
        user_id: int,
        project_id: str,
        prompt: str,
        session_id: str | None = None,
        session_internal_id: int | None = None,
        interactive: bool = False,
    ) -> Task:
        now = utcnow().isoformat()
        cur = await self.conn.execute(
            "INSERT INTO tasks (user_id, project_id, prompt, status, created_at, "
            "session_id, session_internal_id, interactive) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                project_id,
                prompt,
                TaskStatus.PENDING.value,
                now,
                session_id,
                session_internal_id,
                1 if interactive else 0,
            ),
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

    async def list_tasks_by_session(self, session_internal_id: int, limit: int = 20) -> list[Task]:
        cur = await self.conn.execute(
            "SELECT * FROM tasks WHERE session_internal_id = ? ORDER BY id DESC LIMIT ?",
            (session_internal_id, limit),
        )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def count_tasks_in_session(self, session_internal_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE session_internal_id = ?",
            (session_internal_id,),
        )
        row = await cur.fetchone()
        return row["c"]

    async def is_session_busy(self, session_internal_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE session_internal_id = ? AND status = ?",
            (session_internal_id, TaskStatus.RUNNING.value),
        )
        row = await cur.fetchone()
        return row["c"] > 0

    async def find_duplicate(
        self, project_id: str, prompt: str, session_internal_id: int | None = None
    ) -> Task | None:
        if session_internal_id is not None:
            cur = await self.conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND prompt = ? "
                "AND session_internal_id = ? AND status IN ('PENDING', 'RUNNING') "
                "ORDER BY id DESC LIMIT 1",
                (project_id, prompt, session_internal_id),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND prompt = ? "
                "AND session_internal_id IS NULL "
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

    # --- sessions --------------------------------------------------------

    async def create_session(
        self,
        *,
        user_id: int,
        project_id: str,
        opencode_session_id: str | None = None,
        title: str | None = None,
    ) -> Session:
        now = utcnow().isoformat()
        cur = await self.conn.execute(
            "INSERT INTO sessions (user_id, project_id, opencode_session_id, title, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, project_id, opencode_session_id, title, now, now),
        )
        await self.conn.commit()
        return await self.get_session(cur.lastrowid)

    async def get_session(self, session_id: int) -> Session | None:
        cur = await self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        return _row_to_session(row) if row else None

    async def get_session_by_opencode_id(
        self, user_id: int, opencode_session_id: str
    ) -> Session | None:
        cur = await self.conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND opencode_session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, opencode_session_id),
        )
        row = await cur.fetchone()
        return _row_to_session(row) if row else None

    async def list_sessions(self, user_id: int, limit: int = 25) -> list[Session]:
        cur = await self.conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def get_active_session(self, user_id: int) -> Session | None:
        cur = await self.conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND is_active = 1 LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
        return _row_to_session(row) if row else None

    async def set_active_session(self, user_id: int, session_id: int) -> None:
        await self.conn.execute("UPDATE sessions SET is_active = 0 WHERE user_id = ?", (user_id,))
        await self.conn.execute("UPDATE sessions SET is_active = 1 WHERE id = ?", (session_id,))
        await self.conn.commit()

    async def clear_active_session(self, user_id: int) -> None:
        await self.conn.execute("UPDATE sessions SET is_active = 0 WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def touch_session(
        self, session_id: int, *, opencode_session_id: str | None = None
    ) -> None:
        if opencode_session_id is not None:
            await self.conn.execute(
                "UPDATE sessions SET updated_at = ?, opencode_session_id = ? WHERE id = ?",
                (utcnow().isoformat(), opencode_session_id, session_id),
            )
        else:
            await self.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (utcnow().isoformat(), session_id),
            )
        await self.conn.commit()
