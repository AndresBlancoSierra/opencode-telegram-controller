"""Core data models: tasks, projects and shared enums."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


def utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(enum.StrEnum):
    """Lifecycle states of a task."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})

INTERRUPTIBLE_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.RUNNING})


@dataclass
class Task:
    """Persistent metadata for a single OpenCode task."""

    id: int | None
    user_id: int
    project_id: str
    prompt: str
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    session_id: str | None = None
    model: str | None = None
    error: str | None = None
    summary: str | None = None
    log_tail: str | None = None
    commit_created: str | None = None
    session_internal_id: int | None = None
    interactive: bool = False

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


@dataclass
class Session:
    """A persistent OpenCode session owned by a user, scoped to a project.

    The ``opencode_session_id`` (``ses_...``) is assigned on the first message
    and reused by every following message of the same conversation.
    """

    id: int | None
    user_id: int
    project_id: str
    created_at: datetime
    updated_at: datetime
    opencode_session_id: str | None = None
    title: str | None = None
    is_active: bool = False


@dataclass
class Project:
    """A configured, allowlisted workspace that tasks may run in."""

    name: str
    path: Path
    description: str = ""
    enabled: bool = True


@dataclass
class GitState:
    """Snapshot of a repository at a point in time."""

    is_repo: bool = False
    branch: str | None = None
    head: str | None = None
    short_status: list[str] = field(default_factory=list)

    @property
    def is_dirty(self) -> bool:
        return bool(self.short_status)


@dataclass
class TaskResult:
    """Everything collected about a finished task execution."""

    exit_code: int
    session_id: str | None
    export: dict = field(default_factory=dict)
    log_tail: str = ""
    error: str | None = None
