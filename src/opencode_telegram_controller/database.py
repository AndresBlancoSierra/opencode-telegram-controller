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
    commit_created TEXT,
    session_internal_id INTEGER,
    interactive   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    project_id         TEXT    NOT NULL,
    opencode_session_id TEXT,
    title              TEXT,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    is_active          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user_active ON sessions(user_id, is_active);

CREATE TABLE IF NOT EXISTS user_state (
    user_id         INTEGER PRIMARY KEY,
    active_project  TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT,
    params  TEXT,
    result  TEXT NOT NULL,
    error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""

# Columns added to the tasks table by later versions. Applied to pre-existing
# databases without touching task rows (legacy tasks stay consultable).
_TASK_MIGRATIONS = (
    ("session_internal_id", "INTEGER"),
    ("interactive", "INTEGER NOT NULL DEFAULT 0"),
)


async def _apply_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA)
    for column, ddl in _TASK_MIGRATIONS:
        cur = await conn.execute("PRAGMA table_info(tasks)")
        existing = {row["name"] for row in await cur.fetchall()}
        if column not in existing:
            await conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_internal_id)")
    await conn.commit()


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
        await _apply_schema(self._conn)

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
        await _apply_schema(db._conn)
        return db
