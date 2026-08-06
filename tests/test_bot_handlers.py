"""End-to-end tests for the aiogram handlers (Telegram API is mocked)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Chat, Message, Update, User
from conftest import FakeBot, make_settings

from opencode_telegram_controller.auth import AuthorizationService
from opencode_telegram_controller.bot import AppContext, build_router
from opencode_telegram_controller.models import TaskStatus
from opencode_telegram_controller.notifications import NotificationManager
from opencode_telegram_controller.queue_worker import QueueWorker
from opencode_telegram_controller.task_executor import TaskExecutor
from opencode_telegram_controller.task_manager import TaskManager

AUTHORIZED_ID = 123
UNAUTHORIZED_ID = 999


class StubExecutor(TaskExecutor):
    def __init__(self):
        self.cancelled: list[int] = []

    def request_cancel(self, task_id: int) -> None:
        self.cancelled.append(task_id)


def make_ctx(repo, registry):
    settings = make_settings(default_project="A")
    notifier = NotificationManager(FakeBot(), [AUTHORIZED_ID])
    executor = StubExecutor()
    manager = TaskManager(
        repo=repo, registry=registry, notifier=notifier, executor=executor, settings=settings
    )
    auth = AuthorizationService([AUTHORIZED_ID], on_security_event=lambda text: None)
    return AppContext(
        settings=settings,
        repo=repo,
        registry=registry,
        auth=auth,
        manager=manager,
        executor=executor,
        worker=QueueWorker(
            repo=repo, registry=registry, executor=executor, notifier=notifier, settings=settings
        ),
        notifier=notifier,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def sent(monkeypatch):
    """Record Telegram API method calls sent by the bot handlers."""
    messages = []

    async def fake_call(self, method, request_timeout=None):
        messages.append(method.model_dump())
        return None

    monkeypatch.setattr(Bot, "__call__", fake_call)
    return messages


@pytest.fixture
def bot():
    return Bot(token="123:abc", default=DefaultBotProperties())


async def feed(bot, dp, *, user_id=AUTHORIZED_ID, text=None):
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Tester"),
        text=text,
    )
    await dp.feed_update(bot, Update(update_id=1, message=message))


async def build_dp(ctx) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(ctx))
    return dp


async def test_unauthorized_user_rejected(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, user_id=UNAUTHORIZED_ID, text="/status")
    assert sent
    assert "Unauthorized" in sent[0]["text"]


async def test_authorized_user_status(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, user_id=AUTHORIZED_ID, text="/status")
    text = sent[0]["text"]
    assert "Status" in text
    assert "Active project: A" in text
    assert "Running tasks: 0" in text


async def test_natural_language_creates_task(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="Fix the failing tests please")
    text = sent[0]["text"]
    assert "Task #1 created" in text
    assert "Fix the failing tests please" in text
    task = await repo.list_tasks(limit=1)
    assert task and task[0].status == TaskStatus.PENDING


async def test_natural_language_duplicate_rejected(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="same prompt")
    await feed(bot, dp, text="same prompt")
    assert "already queued" in sent[1]["text"]


async def test_natural_language_disabled_project_message(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/use C")
    assert "Unknown or disabled" in sent[-1]["text"]


async def test_use_project(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/use B")
    assert "B" in sent[-1]["text"]
    assert await repo.get_active_project(AUTHORIZED_ID) == "B"


async def test_use_requires_argument(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/use")
    assert "Usage" in sent[-1]["text"]


async def test_tasks_empty(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/tasks")
    assert "No tasks yet" in sent[-1]["text"]


async def test_tasks_lists(repo, registry, sent, bot):
    await repo.create_task(user_id=AUTHORIZED_ID, project_id="A", prompt="hello task")
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/tasks")
    assert "#1" in sent[-1]["text"]
    assert "hello task" in sent[-1]["text"]


async def test_task_detail(repo, registry, sent, bot):
    task = await repo.create_task(user_id=AUTHORIZED_ID, project_id="A", prompt="detail me")
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text=f"/task {task.id}")
    assert "Task #1" in sent[-1]["text"]
    assert "PENDING" in sent[-1]["text"]


async def test_task_missing(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/task 404")
    assert "No task with id 404" in sent[-1]["text"]


async def test_cancel_running_task(repo, registry, sent, bot):
    task = await repo.create_task(user_id=AUTHORIZED_ID, project_id="A", prompt="long")
    await repo.mark_started(task.id)
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text=f"/cancel {task.id}")
    assert f"task #{task.id}" in sent[-1]["text"]
    assert task.id in ctx.executor.cancelled


async def test_cancel_other_users_task_rejected(repo, registry, sent, bot):
    other = await repo.create_task(user_id=AUTHORIZED_ID + 1, project_id="A", prompt="theirs")
    await repo.mark_started(other.id)
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text=f"/cancel {other.id}")
    assert "only cancel your own" in sent[-1]["text"]


async def test_logs_no_tasks(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/logs")
    assert "No tasks yet" in sent[-1]["text"]


async def test_logs_with_tail(repo, registry, sent, bot):
    task = await repo.create_task(user_id=AUTHORIZED_ID, project_id="A", prompt="x")
    await repo.mark_finished(task.id, TaskStatus.COMPLETED, exit_code=0, log_tail="done line")
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text=f"/logs {task.id}")
    assert "done line" in sent[-1]["text"]


async def test_unknown_command(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/bogus")
    assert "Unknown command" in sent[-1]["text"]


async def test_help(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/help")
    assert "OpenCode Telegram Controller" in sent[-1]["text"]
