"""Shared test fixtures. OpenCode execution is always mocked."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opencode_telegram_controller.config import Settings
from opencode_telegram_controller.database import Database
from opencode_telegram_controller.models import GitState
from opencode_telegram_controller.opencode import OpenCodeAdapter, OpenCodeEvent
from opencode_telegram_controller.projects import ProjectRegistry
from opencode_telegram_controller.repository import TaskRepository
from opencode_telegram_controller.task_executor import TaskExecutor


def make_settings(**overrides) -> Settings:
    defaults = dict(
        telegram_bot_token="test-token",
        allowed_user_ids=[123],
        max_concurrent_tasks=1,
        default_timeout_seconds=60,
        progress_interval_seconds=3600,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeRunHandle:
    """Scripted run handle returned by FakeAdapter."""

    def __init__(self, events=None, exit_code=0, stderr=None, wait_delay=0.0):
        self.events_list = list(events or [])
        self.exit_code = exit_code
        self.stderr_lines_out = list(stderr or [])
        self.wait_delay = wait_delay
        self.cancelled = False
        self.killed = False
        self.wait_called = False
        self.process = SimpleNamespace(pid=12345, returncode=None)

    async def events(self):
        for ev in self.events_list:
            yield ev
        if self.process.returncode is None:
            self.process.returncode = self.exit_code

    async def stderr_lines(self) -> list[str]:
        return list(self.stderr_lines_out)

    async def wait(self) -> int:
        self.wait_called = True
        if self.wait_delay:
            await asyncio.sleep(self.wait_delay)
        self.process.returncode = self.exit_code
        return self.exit_code

    async def cancel(self) -> None:
        self.cancelled = True
        if self.process.returncode is None:
            self.process.returncode = 143

    async def kill(self) -> None:
        self.killed = True
        self.process.returncode = 137


class FakeAdapter(OpenCodeAdapter):
    """OpenCode adapter that replays scripted handles."""

    def __init__(self):
        self.script: list[FakeRunHandle] = []
        self.runs: list[dict] = []
        self.exports: dict[str, dict] = {}
        self.run_error: Exception | None = None
        self.missing_sessions: set[str] = set()

    def queue_handle(self, handle: FakeRunHandle) -> FakeAdapter:
        self.script.append(handle)
        return self

    def queue_run(
        self,
        events: list[OpenCodeEvent] | None = None,
        *,
        exit_code: int = 0,
        stderr: list[str] | None = None,
        wait_delay: float = 0.0,
    ) -> FakeRunHandle:
        handle = FakeRunHandle(
            events=events, exit_code=exit_code, stderr=stderr, wait_delay=wait_delay
        )
        self.script.append(handle)
        return handle

    async def run(self, *, prompt: str, cwd: str, session_id: str | None = None):
        if self.run_error is not None:
            raise self.run_error
        if not self.script:
            raise AssertionError("FakeAdapter has no scripted handles")
        handle = self.script.pop(0)
        self.runs.append({"prompt": prompt, "cwd": cwd, "session_id": session_id, "handle": handle})
        return handle

    async def export(self, session_id: str) -> dict:
        return self.exports.get(session_id, {})

    async def session_exists(self, session_id: str) -> bool:
        return session_id not in self.missing_sessions


class FakeNotifier:
    """Records notifications instead of sending them to Telegram."""

    def __init__(self):
        self.messages: list[str] = []
        self.started: list[int] = []
        self.completed: list[int] = []
        self.failed: list[int] = []
        self.cancelled: list[int] = []
        self.progress: list[int] = []
        self.queued: list[int] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)

    async def notify_task_queued(self, task, project) -> None:
        self.queued.append(task.id)
        await self.send(f"queued {task.id}")

    async def notify_task_started(self, task, project) -> None:
        self.started.append(task.id)
        await self.send(f"started {task.id}")

    async def notify_progress(self, task, snippet: str) -> None:
        self.progress.append(task.id)
        await self.send(f"progress {task.id}: {snippet}")

    async def notify_task_completed(self, task, summary) -> None:
        self.completed.append(task.id)
        await self.send(f"completed {task.id}: {summary}")

    async def notify_task_failed(self, task) -> None:
        self.failed.append(task.id)
        await self.send(f"failed {task.id}: {task.error}")

    async def notify_task_cancelled(self, task) -> None:
        self.cancelled.append(task.id)
        await self.send(f"cancelled {task.id}")


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append((chat_id, text))


@pytest.fixture
async def db():
    database = await Database.connect_in_memory()
    yield database
    await database.close()


@pytest.fixture
def repo(db):
    return TaskRepository(db)


@pytest.fixture
def registry(tmp_path):
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_c = tmp_path / "proj-c"
    proj_a.mkdir()
    proj_b.mkdir()
    proj_c.mkdir()
    data = {
        "default_project": "A",
        "projects": [
            {"name": "A", "path": str(proj_a), "description": "Project A"},
            {"name": "B", "path": str(proj_b), "description": "Project B"},
            {"name": "C", "path": str(proj_c), "description": "Project C", "enabled": False},
        ],
    }
    return ProjectRegistry.from_dict(data)


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def adapter():
    return FakeAdapter()


@pytest.fixture
def settings():
    return make_settings()


def make_executor(adapter, repo, registry, notifier, settings) -> TaskExecutor:
    from opencode_telegram_controller.summaries import DeterministicSummaryGenerator

    return TaskExecutor(
        adapter=adapter,
        repo=repo,
        registry=registry,
        notifier=notifier,
        summary_generator=DeterministicSummaryGenerator(),
        settings=settings,
    )


def event(e_type: str, *, session_id: str = "ses_1", text: str | None = None) -> OpenCodeEvent:
    return OpenCodeEvent(type=e_type, session_id=session_id, text=text)


def git_state(**overrides) -> GitState:
    defaults = dict(is_repo=True, branch="main", head="abc1234")
    defaults.update(overrides)
    return GitState(**defaults)
