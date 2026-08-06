"""SQLite persistence layer backed by aiosqlite.

The database stores task history and per-user state (active project). Tasks
survive service restarts. Running tasks at shutdown are marked FAILED with an
interruption error by :meth:`TaskRepository.recover_interrupted`.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
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
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS user_state (
    user_id         INTEGER PRIMARY KEY,
    active_project  TEXT
);
"""


class Database:
    """Thin async wrapper around a single aiosqlite connection."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    @classmethod
    async def connect_in_memory(cls) -> Database:
        db = cls(Path(":memory:"))
        db._conn = await aiosqlite.connect(":memory:")
        db._conn.row_factory = aiosqlite.Row
        await db._conn.executescript(_SCHEMA)
        await db._conn.commit()
        return db
