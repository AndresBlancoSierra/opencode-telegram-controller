"""Tests for notification formatting and sending."""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import FakeBot

from opencode_telegram_controller.formatting import (
    format_duration,
    format_projects_list,
    format_task_detail,
    format_task_line,
    split_text,
)
from opencode_telegram_controller.models import Project, Task, TaskStatus
from opencode_telegram_controller.notifications import NotificationManager


def make_task(**overrides) -> Task:
    defaults = dict(
        id=7,
        user_id=1,
        project_id="A",
        prompt="fix the tests",
        status=TaskStatus.COMPLETED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
        exit_code=0,
        session_id="ses_1",
        summary="All good.",
    )
    defaults.update(overrides)
    return Task(**defaults)


def make_manager(bot=None):
    bot = bot or FakeBot()
    return NotificationManager(bot, chat_ids=[111]), bot


async def test_split_text_small():
    assert split_text("hello") == ["hello"]


async def test_split_text_large_prefers_newlines():
    text = ("a" * 500 + "\n") * 10
    chunks = split_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 4000 for c in chunks)
    assert text.startswith(chunks[0])
    assert text.endswith(chunks[-1])


async def test_split_text_no_newlines_forced():
    text = "x" * 10000
    chunks = split_text(text)
    assert len(chunks) == 3
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(chunks) == text


async def test_completed_notification():
    manager, bot = make_manager()
    await manager.notify_task_completed(make_task(), "All good.")
    assert len(bot.sent) == 1
    _, text = bot.sent[0]
    assert "Task completed" in text
    assert "All good." in text
    assert "2m 0s" in text or "Duration" in text


async def test_failed_notification_includes_error_and_logs():
    manager, bot = make_manager()
    task = make_task(status=TaskStatus.FAILED, error="boom", log_tail="line1\nline2")
    await manager.notify_task_failed(task)
    _, text = bot.sent[0]
    assert "Task failed" in text
    assert "boom" in text
    assert "line2" in text
    assert "ses_1" in text


async def test_started_notification():
    manager, bot = make_manager()
    project = Project(name="A", path=None)
    await manager.notify_task_started(make_task(status=TaskStatus.RUNNING), project)
    assert "OpenCode task started" in bot.sent[0][1]


async def test_progress_snippet_truncated():
    manager, bot = make_manager()
    await manager.notify_progress(make_task(), "z" * 500)
    _, text = bot.sent[0]
    assert len(text) < 400
    assert "..." in text


async def test_send_failure_swallowed():
    class FailingBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("network down")

    manager = NotificationManager(FailingBot(), chat_ids=[111])
    await manager.send("hello")


async def test_send_splits_long_text_to_multiple_messages():
    manager, bot = make_manager()
    await manager.send("x" * 9000)
    assert len(bot.sent) >= 3
    assert all(len(text) <= 4000 for _, text in bot.sent)


async def test_format_duration():
    assert format_duration(5) == "5s"
    assert format_duration(125) == "2m 5s"
    assert format_duration(3661) == "1h 1m 1s"


async def test_format_task_line():
    line = format_task_line(make_task())
    assert "#7" in line
    assert "fix the tests" in line


async def test_format_task_detail():
    detail = format_task_detail(make_task())
    assert "Task #7" in detail
    assert "Project: A" in detail
    assert "COMPLETED" in detail


async def test_format_projects_list():
    projects = [
        Project(name="A", path=None, description="alpha", enabled=True),
        Project(name="B", path=None, enabled=False),
    ]
    text = format_projects_list(projects, active="A")
    assert "A — alpha" in text
    assert "👈 active" in text
    assert "disabled" in text
