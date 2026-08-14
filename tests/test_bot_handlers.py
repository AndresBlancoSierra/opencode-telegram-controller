"""End-to-end tests for the aiogram handlers (Telegram API is mocked)."""

from __future__ import annotations

import asyncio
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
from opencode_telegram_controller.task_manager import TaskManager

AUTHORIZED_ID = 123
UNAUTHORIZED_ID = 999


class StubExecutor:
    """Duck-typed executor: interactive waits resolve immediately."""

    def __init__(self):
        self.cancelled: list[int] = []

    def request_cancel(self, task_id: int) -> None:
        self.cancelled.append(task_id)

    def register_completion_wait(self, task_id: int) -> asyncio.Future[str]:
        future = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(future.set_result, "Stub reply: done")
        return future

    def resolve_completion(self, task_id: int, text: str) -> None:
        pass


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
    assert "Active session: none" in text
    assert "Running tasks: 0" in text


async def test_new_creates_active_session(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/new")
    text = sent[-1]["text"]
    assert "New OpenCode session" in text
    assert "Project: A" in text
    assert (await ctx.manager.active_session(AUTHORIZED_ID)) is not None


async def test_new_with_title(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/new my first session")
    assert "my first session" in sent[-1]["text"]


async def test_history_lists_sessions(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    await ctx.manager.new_session(AUTHORIZED_ID, title="one")
    await ctx.manager.new_session(AUTHORIZED_ID, title="two")
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/history")
    text = sent[-1]["text"]
    assert "#1" in text
    assert "#2" in text
    assert "active" in text


async def test_continue_restores_own_session(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/new")
    first = await ctx.manager.active_session(AUTHORIZED_ID)
    await feed(bot, dp, text="/new")
    await feed(bot, dp, text=f"/continue {first.id}")
    text = sent[-1]["text"]
    assert "OpenCode session restored" in text
    assert (await ctx.manager.active_session(AUTHORIZED_ID)).id == first.id


async def test_continue_requires_argument(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/continue")
    assert "Usage" in sent[-1]["text"]


async def test_use_project_clears_active_session(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    await ctx.manager.new_session(AUTHORIZED_ID)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/use B")
    assert (await ctx.manager.active_session(AUTHORIZED_ID)) is None


async def test_current_without_session(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/current")
    assert "No active session" in sent[-1]["text"]


async def test_current_with_active_session(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    await ctx.manager.new_session(AUTHORIZED_ID)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/current")
    text = sent[-1]["text"]
    assert "Session #1" in text
    assert "Messages: 0" in text


async def test_natural_language_without_session(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="hello")
    assert "No active OpenCode session" in sent[-1]["text"]


async def test_natural_language_uses_active_session(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/new")
    await feed(bot, dp, text="analyze the project")
    text = sent[-1]["text"]
    assert "Stub reply: done" in text
    tasks = await repo.list_tasks(limit=1)
    assert tasks and tasks[0].interactive is True
    assert tasks[0].session_internal_id == (await ctx.manager.active_session(AUTHORIZED_ID)).id


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
    assert "No active session and no tasks yet" in sent[-1]["text"]


async def test_tasks_lists_session_messages(repo, registry, sent, bot):
    ctx = make_ctx(repo, registry)
    session = await ctx.manager.new_session(AUTHORIZED_ID)
    await repo.create_task(
        user_id=AUTHORIZED_ID,
        project_id="A",
        prompt="hello task",
        session_internal_id=session.id,
    )
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/tasks")
    text = sent[-1]["text"]
    assert "#1" in text
    assert "hello task" in text


async def test_tasks_all_shows_legacy(repo, registry, sent, bot):
    await repo.create_task(user_id=AUTHORIZED_ID, project_id="A", prompt="legacy task")
    ctx = make_ctx(repo, registry)
    dp = await build_dp(ctx)
    await feed(bot, dp, text="/tasks all")
    assert "legacy task" in sent[-1]["text"]


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
