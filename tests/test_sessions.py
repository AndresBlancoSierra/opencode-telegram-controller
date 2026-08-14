"""Tests for the persistent session model: creation, continuation, ownership.

These cover the requirements that cannot be asserted through Telegram handlers
alone: real session-id reuse across messages, ownership on /continue, project
switching isolation and OpenCode session availability checks.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import event, make_executor, make_settings

from opencode_telegram_controller.projects import ProjectRegistry
from opencode_telegram_controller.task_manager import SessionError, TaskError, TaskManager

USER_ID = 123


class StubExecutor:
    """Executor whose completion futures are resolved manually."""

    def __init__(self):
        self.waits: dict[int, asyncio.Future[str]] = {}

    def register_completion_wait(self, task_id: int) -> asyncio.Future[str]:
        future = asyncio.get_running_loop().create_future()
        self.waits[task_id] = future
        return future

    def resolve_completion(self, task_id: int, text: str) -> None:
        future = self.waits.pop(task_id, None)
        if future is not None and not future.done():
            future.set_result(text)

    def request_cancel(self, task_id: int) -> None:
        pass


def make_manager(repo, registry, notifier=None, adapter=None, executor=None):
    settings = make_settings(default_timeout_seconds=5, progress_interval_seconds=3600)
    executor = executor or make_executor(None, repo, registry, notifier, settings)
    return TaskManager(
        repo=repo,
        registry=registry,
        notifier=notifier,
        executor=executor,
        settings=settings,
        adapter=adapter,
    )


async def make_active_session(repo, project="A", *, opencode_session_id=None):
    session = await repo.create_session(
        user_id=USER_ID, project_id=project, opencode_session_id=opencode_session_id
    )
    await repo.set_active_session(USER_ID, session.id)
    return session


# --- /new ---------------------------------------------------------------


async def test_new_session_replaces_active_keeps_previous(repo, registry, notifier, adapter):
    manager = make_manager(repo, registry, notifier, adapter)
    first = await manager.new_session(USER_ID)
    assert (await manager.active_session(USER_ID)).id == first.id
    second = await manager.new_session(USER_ID)
    assert (await manager.active_session(USER_ID)).id == second.id
    sessions = await manager.list_sessions(USER_ID)
    assert len(sessions) == 2
    assert (await repo.get_session(first.id)).is_active is False
    assert (await repo.get_session(second.id)).is_active is True


async def test_new_session_requires_active_project(repo, notifier, adapter, tmp_path):
    proj = tmp_path / "proj-a"
    proj.mkdir()
    no_default = ProjectRegistry.from_dict(
        {"projects": [{"name": "A", "path": str(proj), "description": "Project A"}]}
    )
    manager = make_manager(repo, no_default, notifier, adapter)
    with pytest.raises(SessionError, match="No active project"):
        await manager.new_session(USER_ID)


# --- send_message -------------------------------------------------------


async def test_send_message_links_task_to_session(repo, registry, notifier, adapter):
    stub = StubExecutor()
    manager = make_manager(repo, registry, notifier, adapter, executor=stub)
    session = await manager.new_session(USER_ID)
    sender = asyncio.create_task(manager.send_message(USER_ID, session, "analyze cv"))
    await asyncio.sleep(0.05)
    task_id = next(iter(stub.waits))
    stub.resolve_completion(task_id, "the answer")
    reply = await sender
    assert reply == "the answer"
    task = await repo.get_task(task_id)
    assert task.session_internal_id == session.id
    assert task.interactive is True


async def test_send_message_uses_session_opencode_id(repo, registry, notifier, adapter):
    stub = StubExecutor()
    manager = make_manager(repo, registry, notifier, adapter, executor=stub)
    session = await make_active_session(repo, opencode_session_id="ses_xyz")
    sender = asyncio.create_task(manager.send_message(USER_ID, session, "continue here"))
    await asyncio.sleep(0.05)
    task_id = next(iter(stub.waits))
    stub.resolve_completion(task_id, "ok")
    await sender
    task = await repo.get_task(task_id)
    assert task.session_id == "ses_xyz"
    assert task.session_internal_id == session.id


async def test_send_message_rejects_empty_prompt(repo, registry, notifier, adapter):
    stub = StubExecutor()
    manager = make_manager(repo, registry, notifier, adapter, executor=stub)
    session = await manager.new_session(USER_ID)
    with pytest.raises(TaskError, match="cannot be empty"):
        await manager.send_message(USER_ID, session, "   ")


async def test_send_message_rejects_missing_opencode_session(repo, registry, notifier, adapter):
    stub = StubExecutor()
    adapter.missing_sessions.add("ses_gone")
    manager = make_manager(repo, registry, notifier, adapter, executor=stub)
    session = await make_active_session(repo, opencode_session_id="ses_gone")
    with pytest.raises(SessionError, match="no longer exists"):
        await manager.send_message(USER_ID, session, "ping")
    assert not stub.waits


# --- /continue ----------------------------------------------------------


async def test_continue_rejects_foreign_session(repo, registry, notifier, adapter):
    manager = make_manager(repo, registry, notifier, adapter)
    foreign = await repo.create_session(user_id=USER_ID + 1, project_id="A")
    with pytest.raises(SessionError, match="No session matching"):
        await manager.continue_session(USER_ID, str(foreign.id))


async def test_continue_by_opencode_id(repo, registry, notifier, adapter):
    manager = make_manager(repo, registry, notifier, adapter)
    session = await make_active_session(repo, opencode_session_id="ses_abc")
    await manager.new_session(USER_ID)
    restored = await manager.continue_session(USER_ID, "ses_abc")
    assert restored.id == session.id
    assert (await manager.active_session(USER_ID)).id == session.id


async def test_continue_rejects_disabled_project(repo, registry, notifier, adapter):
    manager = make_manager(repo, registry, notifier, adapter)
    session = await make_active_session(repo, project="C")
    with pytest.raises(SessionError, match="no longer enabled"):
        await manager.continue_session(USER_ID, str(session.id))


async def test_continue_rejects_missing_opencode_session(repo, registry, notifier, adapter):
    adapter.missing_sessions.add("ses_gone")
    manager = make_manager(repo, registry, notifier, adapter)
    session = await make_active_session(repo, opencode_session_id="ses_gone")
    with pytest.raises(SessionError, match="no longer exists"):
        await manager.continue_session(USER_ID, "ses_gone")
    assert (await repo.get_session(session.id)).is_active is True


# --- project switching --------------------------------------------------


async def test_set_active_project_clears_active_session(repo, registry, notifier, adapter):
    manager = make_manager(repo, registry, notifier, adapter)
    session = await manager.new_session(USER_ID)
    assert (await manager.active_session(USER_ID)).id == session.id
    await manager.set_active_project(USER_ID, "B")
    assert await manager.active_session(USER_ID) is None
    assert (await repo.get_session(session.id)).is_active is False


async def test_project_switch_never_reuses_other_projects_session(
    repo, registry, notifier, adapter
):
    manager = make_manager(repo, registry, notifier, adapter)
    session = await manager.new_session(USER_ID)  # in project A
    await manager.set_active_project(USER_ID, "B")
    restored = await manager.continue_session(USER_ID, str(session.id))  # still project A
    assert restored.project_id == "A"
    assert (await manager.active_session(USER_ID)).id == session.id


# --- executor level -----------------------------------------------------


async def test_executor_captures_session_id_then_reuses_it(repo, registry, notifier, adapter):
    adapter.queue_run(
        events=[
            event("step_start", session_id="ses_1"),
            event("step_finish", session_id="ses_1"),
        ],
        exit_code=0,
    )
    adapter.exports["ses_1"] = {"info": {}, "messages": []}
    session = await make_active_session(repo)
    executor = make_executor(adapter, repo, registry, notifier, make_settings())
    task = await repo.create_task(
        user_id=USER_ID,
        project_id="A",
        prompt="start",
        session_internal_id=session.id,
        interactive=True,
    )
    future = executor.register_completion_wait(task.id)
    await executor.execute(task.id)
    assert (await repo.get_session(session.id)).opencode_session_id == "ses_1"
    reply = await future
    assert "ses_1" in reply
    task = await repo.get_task(task.id)
    assert task.session_id == "ses_1"


async def test_first_message_reply_announces_new_session(repo, registry, notifier, adapter):
    adapter.queue_run(
        events=[
            event("step_start", session_id="ses_new"),
            event("step_finish", session_id="ses_new"),
        ],
        exit_code=0,
    )
    adapter.exports["ses_new"] = {"info": {}, "messages": []}
    session = await make_active_session(repo)
    executor = make_executor(adapter, repo, registry, notifier, make_settings())
    task = await repo.create_task(
        user_id=USER_ID,
        project_id="A",
        prompt="start",
        session_internal_id=session.id,
        interactive=True,
    )
    future = executor.register_completion_wait(task.id)
    await executor.execute(task.id)
    reply = await future
    assert "ses_new" in reply


async def test_interactive_success_resolves_reply_and_suppresses_spam(
    repo, registry, notifier, adapter
):
    adapter.queue_run(
        events=[
            event("step_start", session_id="ses_1"),
            event("step_finish", session_id="ses_1"),
        ],
        exit_code=0,
    )
    adapter.exports["ses_1"] = {"info": {}, "messages": []}
    executor = make_executor(adapter, repo, registry, notifier, make_settings())
    task = await repo.create_task(
        user_id=USER_ID,
        project_id="A",
        prompt="chat",
        session_internal_id=(await make_active_session(repo)).id,
        interactive=True,
    )
    future = executor.register_completion_wait(task.id)
    await executor.execute(task.id)
    reply = await future
    assert isinstance(reply, str)
    assert notifier.completed == []
    assert notifier.started == []


async def test_interactive_failure_resolves_reply(repo, registry, notifier, adapter):
    adapter.queue_run(
        events=[event("step_start", session_id="ses_1"), event("step_finish")],
        exit_code=1,
        stderr=["boom"],
    )
    executor = make_executor(adapter, repo, registry, notifier, make_settings())
    task = await repo.create_task(
        user_id=USER_ID,
        project_id="A",
        prompt="chat",
        session_internal_id=(await make_active_session(repo)).id,
        interactive=True,
    )
    future = executor.register_completion_wait(task.id)
    await executor.execute(task.id)
    reply = await future
    assert "Task failed" in reply
    assert notifier.failed == []


async def test_cancel_pending_interactive_resolves(repo, registry, notifier, adapter):
    stub = StubExecutor()
    manager = make_manager(repo, registry, notifier, adapter, executor=stub)
    session = await manager.new_session(USER_ID)
    sender = asyncio.create_task(manager.send_message(USER_ID, session, "do work"))
    await asyncio.sleep(0.05)
    task_id = next(iter(stub.waits))
    await manager.cancel_task(USER_ID, task_id)
    reply = await sender
    assert reply == "🛑 Task cancelled"


# --- history / listing --------------------------------------------------


async def test_history_ordering_newest_first(repo, registry, notifier, adapter):
    manager = make_manager(repo, registry, notifier, adapter)
    await manager.new_session(USER_ID)
    await manager.new_session(USER_ID)
    await manager.new_session(USER_ID)
    sessions = await manager.list_sessions(USER_ID)
    assert [s.id for s in sessions] == [3, 2, 1]
